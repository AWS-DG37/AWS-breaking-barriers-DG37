"""Release safety tests: doubles replace Docker/AWS, filesystem state stays real."""

import copy
import importlib
import json
from pathlib import Path

import pytest
import yaml

SHA = "a" * 40
OLD = "b" * 40
DIGEST = "sha256:" + "1" * 64
OLD_IMAGE = "sha256:" + "2" * 64


@pytest.fixture
def d():
    return importlib.import_module("deploy.fieldecho")


@pytest.fixture
def target(d):
    return d.Target.load(Path("deploy/target.json"))


def test_target_rejects_unknown_keys_and_shell_input(d, target):
    raw = vars(target).copy()
    for change in ({"extra": "secret"}, {"instance_id": "i-0;whoami"}, {"region": "us-east-1 x"}):
        with pytest.raises(ValueError):
            d.Target.parse({**raw, **change})


@pytest.mark.parametrize("sha,sequence", [("main", 1), (SHA, 0), (SHA, True), (SHA + ";id", 1)])
def test_invalid_release_never_reaches_aws(d, target, monkeypatch, sha, sequence):
    monkeypatch.setattr(d, "aws", lambda *a, **k: pytest.fail("AWS reached"))
    with pytest.raises(ValueError):
        d.deploy(target, sha, sequence)


def test_superseded_main_never_dispatches(d, target, monkeypatch):
    monkeypatch.setattr(d, "main_head", lambda t: OLD)
    monkeypatch.setattr(d, "aws", lambda *a, **k: pytest.fail("AWS reached"))
    assert d.deploy(target, SHA, 5)["outcome"] == "SUPERSEDED"


def test_deploy_and_status_use_fixed_document_version(d, target, monkeypatch):
    requests = []

    def aws(t, service, operation, **kwargs):
        requests.append((service, operation, kwargs))
        if operation == "describe-images":
            return {"imageDetails": [{"imageDigest": DIGEST}]}
        if operation == "send-command":
            return {"Command": {"CommandId": "command-1"}}
        return {"Status": "Success", "StandardOutputContent": 'DG37_RESULT {"outcome":"DEPLOYED"}'}

    monkeypatch.setattr(d, "aws", aws)
    monkeypatch.setattr(d, "main_head", lambda t: SHA)
    assert d.deploy(target, SHA, 5)["outcome"] == "DEPLOYED"
    d.status(target)
    sent = [kw for _, op, kw in requests if op == "send-command"]
    assert len(sent) == 2
    assert all(x["document_name"] == target.ssm_document for x in sent)
    assert all(x["document_version"] == target.ssm_document_version for x in sent)
    assert sent[0]["parameters"]["Action"] == ["deploy"]
    assert sent[1]["parameters"]["Action"] == ["status"]
    assert all(x["instance_ids"] == [target.instance_id] for x in sent)


@pytest.mark.parametrize(
    "status,body,auth,worker,passes",
    [
        (200, {"database": "connected"}, False, False, True),
        (200, {"database": "unavailable"}, False, False, False),
        (401, {}, True, False, True),
        (200, {"database": "connected"}, True, False, False),
        (401, {}, False, False, False),
        (401, {}, True, True, False),
    ],
)
def test_readiness_needs_real_db_auth_and_stopped_worker(d, status, body, auth, worker, passes):
    proof = {"database": "connected", "live_enabled": False, "auth_required": auth}
    if passes:
        d.check_readiness(proof, status, body, [200, 200], worker)
    else:
        with pytest.raises(ValueError):
            d.check_readiness(proof, status, body, [200, 200], worker)


def test_database_failure_is_fatal_even_with_auth_401(d):
    with pytest.raises(ValueError):
        d.check_readiness(
            {"database": "unavailable", "live_enabled": False, "auth_required": True},
            401,
            {},
            [200, 200],
            False,
        )


def test_migrations_preserve_unknown_old_filename_and_recorded_data(d):
    before = {
        "ledger": {"001.sql": "a", "003_website_usage.sql": "old"},
        "tables": {
            "website_usage": {"rows": 3, "hash": "same"},
            "generation_runs": {"rows": 1, "hash": "recorded"},
        },
    }
    after = copy.deepcopy(before)
    after["ledger"]["003_configs_datasets_jobs.sql"] = "new"
    d.check_snapshot(before, after, {"001.sql": "a", "003_configs_datasets_jobs.sql": "new"})
    after["tables"]["generation_runs"]["hash"] = "lost"
    with pytest.raises(ValueError):
        d.check_snapshot(before, after, {"003_configs_datasets_jobs.sql": "new"})


class Runtime:
    """Docker boundary fixture; a crash is BaseException, outside ordinary rollback."""

    def __init__(self, root, fail=None):
        self.root, self.fail, self.events = root, fail, []
        self.running = OLD_IMAGE

    def adopt(self):
        return {"sha": OLD, "image": OLD_IMAGE, "sequence": 0}

    def preflight(self, release):
        self.events.append("preflight")

    def rehearse_and_migrate(self, release, previous):
        self.events.extend(
            [
                "snapshot",
                "restore",
                "migrate-copy-1",
                "migrate-copy-2",
                "previous-compatible",
                "migrate-production",
            ]
        )

    def switch(self, compose):
        self.running = json.loads(compose)["services"]["explorer"]["image"]
        self.events.append("switch:" + self.running)

    def smoke(self, release):
        if self.fail == "smoke" and release["sha"] == SHA:
            raise ValueError("candidate unhealthy")
        self.events.append("smoke:" + release["sha"])

    def serving_image(self):
        return self.running

    def recover_cleanup(self, candidate):
        self.events.append("cleanup")

    def compose(self, release):
        return json.dumps({"services": {"explorer": {"image": release["image"]}}})


@pytest.fixture
def host(tmp_path, d, target):
    runtime = Runtime(tmp_path)
    (tmp_path / "compose.yaml").write_text(runtime.compose(runtime.adopt()))
    return d.Host(tmp_path, target, runtime), runtime


def test_success_serves_digest_and_replay_cannot_undo_it(d, target, host):
    h, r = host
    release = d.Release.parse(
        {"sha": SHA, "image": target.registry + "@" + DIGEST, "sequence": 7}, target
    )
    assert h.activate(release)["outcome"] == "DEPLOYED"
    assert r.events.index("migrate-production") < r.events.index("switch:" + release.image)
    r.events.clear()
    assert h.activate(release)["outcome"] == "REFUSED_STALE"
    assert not r.events
    assert h.read().highest_sequence == 7


def test_failed_switch_restores_previous_and_keeps_sequence(d, target, host):
    h, r = host
    r.fail = "smoke"
    release = d.Release.parse(
        {"sha": SHA, "image": target.registry + "@" + DIGEST, "sequence": 8}, target
    )
    assert h.activate(release)["outcome"] == "FAILED_ROLLED_BACK"
    assert r.running == OLD_IMAGE
    assert h.read().highest_sequence == 8
    assert h.read().transition is None
    assert (
        json.loads((h.root / "compose.yaml").read_text())["services"]["explorer"]["image"]
        == OLD_IMAGE
    )


@pytest.mark.parametrize("window", ["intent", "switched", "compose"])
def test_crash_recovers_last_healthy_compose_before_fencing(d, target, host, window):
    h, r = host

    class Crash(BaseException):
        pass

    h.checkpoint = lambda name: (_ for _ in ()).throw(Crash()) if name == window else None
    release = d.Release.parse(
        {"sha": SHA, "image": target.registry + "@" + DIGEST, "sequence": 9}, target
    )
    with pytest.raises(Crash):
        h.activate(release)
    h.checkpoint = lambda name: None
    assert h.activate(release)["outcome"] == "REFUSED_STALE"
    assert r.running == OLD_IMAGE
    assert h.read().highest_sequence == 9
    assert h.read().transition is None


def test_workflow_mismatch_fails_without_switch(d, target, host):
    h, r = host
    release = d.Release.parse(
        {"sha": SHA, "image": target.registry + "@" + DIGEST, "sequence": 10}, target
    )
    h.activate(release)
    state = json.loads((h.root / "delivery-state.json").read_text())
    state["workflow"] = ".github/workflows/renamed.yml"
    (h.root / "delivery-state.json").write_text(json.dumps(state))
    r.events.clear()
    with pytest.raises(ValueError):
        h.activate(release)
    assert not r.events


def test_compose_preserves_database_env_volumes_and_explicit_no_live(d):
    old = {
        "services": {
            "postgres": {"image": "postgres:16", "env_file": "database.env"},
            "explorer": {
                "build": {"context": "old"},
                "env_file": "explorer.env",
                "volumes": ["/data/pack:/data/pack:ro"],
            },
        },
        "volumes": {"postgres-data": {}},
    }
    new = d.render_compose(old, "image@" + DIGEST)
    assert new["services"]["postgres"] == old["services"]["postgres"]
    assert new["volumes"] == {"postgres-data": {}}
    explorer = new["services"]["explorer"]
    assert explorer["entrypoint"] == [
        "mel-explorer",
        "--host",
        "127.0.0.1",
        "--port",
        "8877",
        "--no-live",
    ]
    assert explorer["env_file"] == "explorer.env"
    assert explorer["volumes"] == ["/data/pack:/data/pack:ro"]
    assert "build" not in explorer
    with pytest.raises(ValueError):
        d.render_compose({"services": {**old["services"], "worker": {"image": "worker"}}}, "image")


def test_main_trigger_and_iam_are_scoped_to_admitted_release():
    workflow = yaml.safe_load(Path(".github/workflows/main.yml").read_text())
    triggers = workflow.get("on", workflow.get(True))
    assert triggers == {"pull_request": None, "push": {"branches": ["main"]}}
    assert workflow["concurrency"]["cancel-in-progress"] is False
    release = workflow["jobs"]["release"]
    assert release["if"] == "github.event_name == 'push' && github.ref == 'refs/heads/main'"
    assert "id-token" not in workflow["jobs"]["check"].get("permissions", {})
    template = yaml.load(Path("deploy/bootstrap.yaml").read_text(), Loader=yaml.BaseLoader)
    role = template["Resources"]["DeployRole"]["Properties"]
    trust = role["AssumeRolePolicyDocument"]["Statement"][0]["Condition"]["StringEquals"]
    assert (
        trust["token.actions.githubusercontent.com:sub"]
        == "repo:AWS-DG37@352139567/AWS-breaking-barriers-DG37@1444724104:ref:refs/heads/main"
    )
    assert trust["token.actions.githubusercontent.com:aud"] == "sts.amazonaws.com"
    policies = json.dumps(role["Policies"])
    assert "AWS-RunShellScript" not in policies
    assert "bedrock:" not in policies
    assert "iam:" not in policies
    statements = role["Policies"][0]["PolicyDocument"]["Statement"]
    send = next(x for x in statements if x["Action"] == "ssm:SendCommand")
    assert len(send["Resource"]) == 2
    assert all("*" not in json.dumps(resource) for resource in send["Resource"])


def test_wrong_artifact_revision_fails_before_any_container_run(d, target, tmp_path, monkeypatch):
    runtime = d.DockerHost.__new__(d.DockerHost)
    runtime.root, runtime.target = tmp_path, target
    runtime.current = {"services": {"postgres": {}, "explorer": {}}}
    (tmp_path / "explorer.env").touch()
    (tmp_path / "database.env").touch()
    monkeypatch.setattr(runtime, "worker_present", lambda: False)
    monkeypatch.setattr(
        runtime,
        "inspect",
        lambda image: {"Config": {"Labels": {"org.opencontainers.image.revision": OLD}}},
    )
    monkeypatch.setattr(runtime, "run_image", lambda *a: pytest.fail("invalid artifact ran"))
    with pytest.raises(ValueError, match="revision"):
        runtime.preflight({"sha": SHA, "image": target.registry + "@" + DIGEST, "sequence": 1})


def test_subprocess_failure_never_echoes_secret_output(d, monkeypatch):
    import subprocess

    monkeypatch.setattr(
        d.subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(
            a, 1, "private credential", "password=private"
        ),
    )
    with pytest.raises(RuntimeError) as error:
        d.command(["docker", "exec", "container"])
    assert "private" not in str(error.value)
    assert "password" not in str(error.value)


def test_invalid_persisted_release_is_refused_before_switch(d, target, host):
    h, r = host
    state = {
        "deployed": {"sha": "main", "image": "arbitrary", "sequence": 0},
        "previous": None,
        "highest_sequence": 5,
        "workflow": target.workflow,
        "transition": None,
        "last_outcome": "DEPLOYED",
    }
    (h.root / "delivery-state.json").write_text(json.dumps(state))
    release = d.Release.parse(
        {"sha": SHA, "image": target.registry + "@" + DIGEST, "sequence": 10}, target
    )
    with pytest.raises(ValueError):
        h.activate(release)
    assert r.events == []


def test_failed_ssm_activation_still_returns_structured_serving_outcome(d, target, monkeypatch):
    def aws(t, service, operation, **kwargs):
        if operation == "send-command":
            return {"Command": {"CommandId": "command-1"}}
        return {
            "Status": "Failed",
            "StandardOutputContent": "DG37_RESULT "
            + json.dumps({"outcome": "FAILED_ROLLED_BACK", "deployed": {"sha": OLD}}),
        }

    monkeypatch.setattr(d, "aws", aws)
    result = d.status(target)
    assert result["outcome"] == "FAILED_ROLLED_BACK"
    assert result["deployed"]["sha"] == OLD


def test_success_reports_actual_serving_identity(d, target, host):
    h, r = host
    release = d.Release.parse(
        {"sha": SHA, "image": target.registry + "@" + DIGEST, "sequence": 1}, target
    )
    assert h.activate(release)["serving_image"] == target.registry + "@" + DIGEST


def test_status_read_never_adopts_or_tags_an_unmanaged_host(host):
    h, _ = host
    with pytest.raises(ValueError, match="managed"):
        h.read(adopt=False)
    assert not h.state_path.exists()


def test_migration_failure_recovers_previous_before_consuming_next_release(d, target, host):
    h, r = host

    def migration_failed(*args):
        r.events.append("snapshot")
        raise RuntimeError("rehearsal unavailable")

    r.rehearse_and_migrate = migration_failed
    release = d.Release.parse(
        {"sha": SHA, "image": target.registry + "@" + DIGEST, "sequence": 2}, target
    )
    result = h.activate(release)
    assert result["outcome"] == "FAILED_BEFORE_SWITCH"
    assert "smoke:" + OLD in r.events
    assert h.read().highest_sequence == 2
    assert h.read().transition is None


def test_container_probe_programs_are_executable_python(d):
    for program in (d.DB_PROBE, d.HTTP_PROBE, d.SNAPSHOT, d.MIGRATE):
        compile(program, "<container-probe>", "exec")


def test_failed_final_state_persistence_recovers_previous_image(d, target, host):
    h, r = host
    save = h.save

    def disk_full_on_candidate(state):
        if state.last_outcome == "DEPLOYED":
            raise OSError("state write failed")
        save(state)

    h.save = disk_full_on_candidate
    release = d.Release.parse(
        {"sha": SHA, "image": target.registry + "@" + DIGEST, "sequence": 12}, target
    )
    result = h.activate(release)
    assert result["outcome"] == "FAILED_ROLLED_BACK"
    assert result["serving_image"] == OLD_IMAGE
    assert result["deployed"]["sha"] == OLD
    assert h.read().transition is None
    assert h.read().highest_sequence == 12
