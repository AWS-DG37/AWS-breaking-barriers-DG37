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
    def __init__(self, root, fail=None):
        self.root, self.fail, self.events = root, fail, []
        self.running = OLD_IMAGE

    def adopt(self):
        return {"sha": OLD, "image": OLD_IMAGE, "sequence": 0}

    def preflight(self, release):
        self.events.append("preflight")
        if self.fail == "preflight":
            raise ValueError("artifact temporarily unavailable")

    def rehearse_and_migrate(self, release, previous):
        if self.fail == "migration":
            raise ValueError("rehearsal temporarily unavailable")
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
        if self.running != release["image"]:
            raise ValueError("serving image differs from release")
        if self.fail == "smoke" and release["sha"] == SHA:
            raise ValueError("candidate unhealthy")
        self.events.append("smoke:" + release["sha"])

    def serving_image(self):
        return self.running

    def recover_cleanup(self, candidate):
        self.events.append("cleanup")

    def compose(self, release):
        return json.dumps({"services": {"explorer": {"image": release["image"]}}})

    def validate_compose(self, compose, name):
        self.events.append("validate:" + name)
        if self.fail == "compose-" + name:
            raise ValueError("Compose rejected rendered service")


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
    assert h.activate(release)["outcome"] == "ALREADY_DEPLOYED"
    assert r.events == ["smoke:" + SHA]
    assert h.read().highest_sequence == 7


@pytest.mark.parametrize("failure", ["image", "readiness"])
def test_active_replay_verifies_serving_image_and_readiness(d, target, host, failure):
    h, r = host
    release = d.Release.parse(
        {"sha": SHA, "image": target.registry + "@" + DIGEST, "sequence": 7}, target
    )
    h.activate(release)
    if failure == "image":
        r.running = OLD_IMAGE
    else:
        r.fail = "smoke"
    r.events.clear()
    with pytest.raises(ValueError):
        h.activate(release)
    assert not r.events
    assert h.read().deployed == vars(release)


@pytest.mark.parametrize("failure", ["preflight", "migration", "smoke"])
def test_exact_failed_release_can_retry_after_host_restart(d, target, host, failure):
    h, r = host
    release = d.Release.parse(
        {"sha": SHA, "image": target.registry + "@" + DIGEST, "sequence": 8}, target
    )
    r.fail = failure
    assert h.activate(release)["outcome"].startswith("FAILED_")
    r.fail = None
    h = d.Host(h.root, target, r)
    assert h.activate(release)["outcome"] == "DEPLOYED"
    assert r.running == release.image
    assert h.read().highest_sequence == 8


def test_admission_crash_can_retry_same_release_without_lowering_fence(d, target, host):
    h, r = host
    save = h.save

    class Crash(BaseException):
        pass

    def crash_after_admission(state):
        save(state)
        raise Crash()

    h.save = crash_after_admission
    release = d.Release.parse(
        {"sha": SHA, "image": target.registry + "@" + DIGEST, "sequence": 8}, target
    )
    with pytest.raises(Crash):
        h.activate(release)
    h = d.Host(h.root, target, r)
    stale = d.Release.parse({**vars(release), "sequence": 7}, target)
    assert h.activate(stale)["outcome"] == "REFUSED_STALE"
    assert h.activate(release)["outcome"] == "DEPLOYED"
    assert h.read().highest_sequence == 8


@pytest.mark.parametrize("failed", [False, True])
@pytest.mark.parametrize("change", [{"sequence": 6}, {"sha": OLD}, {"image": "changed"}])
def test_lower_sequence_and_same_sequence_identity_collision_are_refused(
    d, target, host, failed, change
):
    h, r = host
    release = d.Release.parse(
        {"sha": SHA, "image": target.registry + "@" + DIGEST, "sequence": 7}, target
    )
    r.fail = "preflight" if failed else None
    h.activate(release)
    if change.get("image") == "changed":
        change = {"image": target.registry + "@sha256:" + "3" * 64}
    other = d.Release.parse({**vars(release), **change}, target)
    r.events.clear()
    h = d.Host(h.root, target, r)
    assert h.activate(other)["outcome"] == "REFUSED_STALE"
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
    r.events.clear()
    assert h.activate(release)["outcome"] == "DEPLOYED"
    assert r.events.index("smoke:" + OLD) < r.events.index("preflight")
    assert r.running == release.image
    assert h.read().highest_sequence == 9
    assert h.read().transition is None


@pytest.mark.parametrize("window", ["intent", "switched", "compose"])
def test_crash_recovery_preserves_fence_against_other_release(d, target, host, window):
    h, r = host

    class Crash(BaseException):
        pass

    h.checkpoint = lambda name: (_ for _ in ()).throw(Crash()) if name == window else None
    release = d.Release.parse(
        {"sha": SHA, "image": target.registry + "@" + DIGEST, "sequence": 9}, target
    )
    with pytest.raises(Crash):
        h.activate(release)
    h = d.Host(h.root, target, r)
    for change in (
        {"sequence": 8},
        {"sha": OLD},
        {"image": target.registry + "@sha256:" + "3" * 64},
    ):
        assert (
            h.activate(d.Release.parse({**vars(release), **change}, target))["outcome"]
            == "REFUSED_STALE"
        )
    assert r.running == OLD_IMAGE
    assert h.read().highest_sequence == 9
    assert h.read().transition is None


@pytest.mark.parametrize("window", ["switch", "smoke", "save"])
def test_interrupted_recovery_can_resume_and_retry_exact_release(d, target, host, window):
    h, r = host

    class Crash(BaseException):
        pass

    h.checkpoint = lambda name: (_ for _ in ()).throw(Crash())
    release = d.Release.parse(
        {"sha": SHA, "image": target.registry + "@" + DIGEST, "sequence": 9}, target
    )
    with pytest.raises(Crash):
        h.activate(release)
    h = d.Host(h.root, target, r)
    owner = h if window == "save" else r
    operation = getattr(owner, window)

    def crash_during_recovery(*args):
        if window != "save":
            operation(*args)
        raise Crash()

    setattr(owner, window, crash_during_recovery)
    with pytest.raises(Crash):
        h.activate(release)
    assert h.read().highest_sequence == 9
    assert h.read().transition is not None
    setattr(owner, window, operation)
    assert d.Host(h.root, target, r).activate(release)["outcome"] == "DEPLOYED"
    assert r.running == release.image


def test_rehearsal_retries_preserve_each_attempt_backup(d, target, tmp_path, monkeypatch):
    runtime = d.DockerHost.__new__(d.DockerHost)
    runtime.root, runtime.target, runtime.container = tmp_path, target, "explorer"
    (tmp_path / "database.env").write_text("POSTGRES_USER=app\nPOSTGRES_DB=app\n")
    before = {"ledger": {}, "tables": {}, "columns": {}}
    dumps = iter(["first backup", "second backup"])
    monkeypatch.setattr(runtime, "compose_command", lambda *a: "")
    monkeypatch.setattr(runtime, "smoke", lambda *a: None)
    monkeypatch.setattr(d, "command", lambda *a, **kw: "")
    monkeypatch.setattr(
        runtime,
        "run_image",
        lambda image, script, *a, **kw: json.dumps(before) if script == d.SNAPSHOT else "{}",
    )
    monkeypatch.setattr(
        runtime, "pg", lambda action, *a, **kw: next(dumps) if action == "pg_dump" else ""
    )
    release = {"sha": SHA, "image": target.registry + "@" + DIGEST, "sequence": 4}
    previous = {"sha": OLD, "image": OLD_IMAGE, "sequence": 0}
    backups = tmp_path / "backups" / ("4-" + SHA)
    backups.mkdir(parents=True)
    (backups / "database.sql").write_text("legacy backup")
    runtime.rehearse_and_migrate(release, previous)
    runtime.rehearse_and_migrate(release, previous)
    assert (backups / "database.sql").read_text() == "legacy backup"
    attempts = [path for path in backups.iterdir() if path.is_dir()]
    assert len(attempts) == 2
    assert {(attempt / "database.sql").read_text() for attempt in attempts} == {
        "first backup",
        "second backup",
    }
    assert all((attempt / "migration-proof.json").is_file() for attempt in attempts)
    assert all(attempt.stat().st_mode & 0o777 == 0o700 for attempt in attempts)


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
    h.save = save
    assert h.activate(release)["outcome"] == "DEPLOYED"


@pytest.mark.parametrize("transition", [False, True])
def test_legacy_state_replays_only_evidenced_highest_release(d, target, host, transition):
    h, r = host
    release = d.Release.parse(
        {"sha": SHA, "image": target.registry + "@" + DIGEST, "sequence": 7}, target
    )
    if transition:

        class Crash(BaseException):
            pass

        h.checkpoint = lambda name: (_ for _ in ()).throw(Crash())
        with pytest.raises(Crash):
            h.activate(release)
    else:
        h.activate(release)
    raw = json.loads(h.state_path.read_text())
    raw.pop("highest_release")
    h.state_path.write_text(json.dumps(raw))
    h = d.Host(h.root, target, r)
    assert h.activate(release)["outcome"] == ("DEPLOYED" if transition else "ALREADY_DEPLOYED")


def test_legacy_failed_admission_with_unknown_identity_remains_fenced(d, target, host):
    h, r = host
    release = d.Release.parse(
        {"sha": SHA, "image": target.registry + "@" + DIGEST, "sequence": 7}, target
    )
    r.fail = "preflight"
    h.activate(release)
    raw = json.loads(h.state_path.read_text())
    raw.pop("highest_release")
    h.state_path.write_text(json.dumps(raw))
    r.fail = None
    r.events.clear()
    assert h.activate(release)["outcome"] == "REFUSED_STALE"
    assert not r.events


@pytest.mark.parametrize("change", [{"sequence": 6}, {"image": "invalid"}])
def test_invalid_admitted_identity_is_refused_before_runtime(d, target, host, change):
    h, r = host
    release = d.Release.parse(
        {"sha": SHA, "image": target.registry + "@" + DIGEST, "sequence": 7}, target
    )
    h.activate(release)
    raw = json.loads(h.state_path.read_text())
    raw["highest_release"].update(change)
    h.state_path.write_text(json.dumps(raw))
    r.events.clear()
    with pytest.raises(ValueError):
        h.activate(release)
    assert not r.events


def test_already_deployed_is_a_successful_host_command(d, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(d, "ROOT", tmp_path)
    monkeypatch.setattr(d.os, "umask", lambda mode: 0o022)
    monkeypatch.setattr(d, "DockerHost", lambda *a: object())
    monkeypatch.setattr(d.Host, "activate", lambda *a: {"outcome": "ALREADY_DEPLOYED"})
    monkeypatch.setattr(
        d.sys,
        "argv",
        ["fieldecho.py", "host-activate", "--sha", SHA, "--digest", DIGEST, "--sequence", "7"],
    )
    assert d.main() == 0
    assert '"outcome": "ALREADY_DEPLOYED"' in capsys.readouterr().out


@pytest.mark.parametrize("failure", ["healthy", "candidate"])
def test_invalid_rendered_compose_preserves_original_before_any_switch(d, target, host, failure):
    h, r = host
    original = "# exact operator compose\n" + (h.root / "compose.yaml").read_text()
    (h.root / "compose.yaml").write_text(original)
    r.fail = "compose-" + failure
    release = d.Release(SHA, target.registry + "@" + DIGEST, 13)
    assert h.activate(release)["outcome"] == "FAILED_BEFORE_SWITCH"
    assert (h.root / "compose.yaml").read_text() == original
    assert not any(event.startswith("switch:") for event in r.events)
    assert "snapshot" not in r.events


def test_actual_healthy_compose_is_proved_before_migration_and_original_is_private(
    d, target, host
):
    h, r = host
    original = (h.root / "compose.yaml").read_text()
    release = d.Release(SHA, target.registry + "@" + DIGEST, 13)
    assert h.activate(release)["outcome"] == "DEPLOYED"
    assert r.events.index("validate:healthy") < r.events.index("switch:" + OLD_IMAGE)
    assert r.events.index("validate:candidate") < r.events.index("switch:" + OLD_IMAGE)
    assert r.events.index("switch:" + OLD_IMAGE) < r.events.index("smoke:" + OLD)
    assert r.events.index("smoke:" + OLD) < r.events.index("snapshot")
    backup = h.root / "compose.original.yaml"
    assert backup.read_text() == original
    assert backup.stat().st_mode & 0o777 == 0o600
    newer = d.Release("c" * 40, target.registry + "@sha256:" + "3" * 64, 14)
    assert h.activate(newer)["outcome"] == "DEPLOYED"
    assert backup.read_text() == original


@pytest.mark.parametrize(
    "change",
    [
        {"network_mode": "bridge"},
        {"networks": {"default": None}},
        {"ports": ["8877:8877"]},
        {"environment": {"MEL_DATABASE_HOST": "postgres"}},
    ],
)
def test_incompatible_host_layout_fails_before_image_probe(d, target, tmp_path, monkeypatch, change):
    runtime = d.DockerHost.__new__(d.DockerHost)
    runtime.root, runtime.target = tmp_path, target
    runtime.current = {
        "services": {
            "postgres": {"image": "postgres:16"},
            "explorer": {"image": OLD_IMAGE, "network_mode": "host", **change},
        }
    }
    (tmp_path / "explorer.env").write_text("MEL_DATABASE_HOST=127.0.0.1\n")
    (tmp_path / "database.env").touch()
    monkeypatch.setattr(runtime, "worker_present", lambda: False)
    monkeypatch.setattr(runtime, "run_image", lambda *a: pytest.fail("host not validated"))
    monkeypatch.setattr(runtime, "inspect", lambda *a: {"Config": {"Labels": {
        "org.opencontainers.image.revision": SHA}}})
    monkeypatch.setattr(runtime, "compose_command", lambda *a, **k: json.dumps({
        "services": {"explorer": {"environment": {"MEL_DATABASE_HOST": "127.0.0.1"}}}}))
    with pytest.raises(ValueError):
        runtime.preflight({"sha": SHA, "image": target.registry + "@" + DIGEST, "sequence": 1})


def test_compose_validation_runs_installed_parser_on_both_saved_files(
    d, target, tmp_path, monkeypatch
):
    runtime = d.DockerHost.__new__(d.DockerHost)
    runtime.root, runtime.target = tmp_path, target
    calls = []
    monkeypatch.setattr(runtime, "compose_command", lambda *a, **k: calls.append((a, k)))
    for name, image in (("healthy", OLD_IMAGE), ("candidate", target.registry + "@" + DIGEST)):
        rendered = json.dumps({"services": {"explorer": {"image": image}}})
        runtime.validate_compose(rendered, name)
        filename = "compose." + name + ".json"
        assert (tmp_path / filename).read_text() == rendered
        assert calls[-1] == (("config", "--quiet"), {"file": filename})


def test_timeout_after_stop_recovers_prior_release_without_another_admission(
    d, target, host, monkeypatch
):
    h, r = host
    clock = [100.0]
    monkeypatch.setattr(d.time, "monotonic", lambda: clock[0])
    budget = d.ActivationDeadline(110.0, 130.0)
    monkeypatch.setattr(d, "HOST_DEADLINE", budget)

    def timeout_after_stop(*args):
        r.running = None
        clock[0] = 111.0
        d.command(["docker", "compose", "exec", "postgres"])

    r.rehearse_and_migrate = timeout_after_stop
    release = d.Release(SHA, target.registry + "@" + DIGEST, 15)
    assert h.activate(release)["outcome"] == "FAILED_BEFORE_SWITCH"
    assert r.running == OLD_IMAGE
    assert h.read().highest_sequence == 15
    assert h.read().transition is None
    assert json.loads((h.root / "compose.yaml").read_text())["services"]["explorer"]["image"] == OLD_IMAGE


def test_failed_timeout_recovery_keeps_intent_and_fence(d, target, host, monkeypatch):
    h, r = host
    monkeypatch.setattr(d, "HOST_DEADLINE", d.ActivationDeadline(0, 0))
    release = d.Release(SHA, target.registry + "@" + DIGEST, 15)

    def timeout_after_stop(*args):
        r.running = None
        raise d.ActivationInterrupted("deadline")

    def no_recovery(*args):
        raise d.ActivationInterrupted("recovery deadline")

    r.rehearse_and_migrate = timeout_after_stop
    r.recover_cleanup = no_recovery
    with pytest.raises(d.ActivationInterrupted):
        h.activate(release)
    assert h.read().transition is not None
    assert h.read().highest_sequence == 15
    assert h.read().highest_release == vars(release)


def test_subprocess_timeout_uses_remaining_overall_budget(d, monkeypatch):
    import subprocess

    monkeypatch.setattr(d.time, "monotonic", lambda: 100.0)
    budget = d.ActivationDeadline(110.0, 130.0)
    monkeypatch.setattr(d, "HOST_DEADLINE", budget)
    timeouts = []

    def run(*a, **kw):
        timeouts.append(kw["timeout"])
        return subprocess.CompletedProcess(a, 0, "ok", "")

    monkeypatch.setattr(d.subprocess, "run", run)
    assert d.command(["docker", "inspect"]) == "ok"
    budget.recover()
    assert d.command(["docker", "inspect"]) == "ok"
    assert timeouts == [10.0, 30.0]


def test_deadline_accounts_for_document_startup_time(d, monkeypatch):
    monkeypatch.setattr(d.time, "time", lambda: 1000.0)
    monkeypatch.setattr(d.time, "monotonic", lambda: 100.0)
    monkeypatch.setenv("FIELDECHO_ACTIVATE_BY", "1100")
    monkeypatch.setenv("FIELDECHO_RECOVER_BY", "1460")
    budget = d.ActivationDeadline.from_environment()
    assert budget.remaining() == 100.0
    budget.recover()
    assert budget.remaining() == 460.0


def test_termination_signal_is_a_recoverable_exception(d):
    with pytest.raises(d.ActivationInterrupted):
        d.interrupt_activation(15, None)


def test_rejected_and_failed_artifacts_cannot_change_status_handler(d, target, host):
    h, r = host
    directory = h.root / "delivery"
    directory.mkdir()
    (directory / "current").symlink_to("releases/" + OLD)
    r.fail = "preflight"
    release = d.Release(SHA, target.registry + "@" + DIGEST, 16)
    assert h.activate(release)["outcome"] == "FAILED_BEFORE_SWITCH"
    assert (directory / "current").readlink() == Path("releases/" + OLD)
    assert h.activate(d.Release(SHA, release.image, 15))["outcome"] == "REFUSED_STALE"
    assert (directory / "current").readlink() == Path("releases/" + OLD)
    r.fail = None
    assert h.activate(release)["outcome"] == "DEPLOYED"
    assert (directory / "current").readlink() == Path("releases/" + SHA)
