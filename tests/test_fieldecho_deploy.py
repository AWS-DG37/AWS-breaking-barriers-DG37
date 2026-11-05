import copy
import importlib
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

SHA = "a" * 40
OLD = "b" * 40
DIGEST = "sha256:" + "1" * 64
OLD_IMAGE = "sha256:" + "2" * 64


@pytest.fixture
def managed_proxy(tmp_path, monkeypatch):
    from deploy import ec2_setup

    installed = tmp_path / "nginx.conf"
    monkeypatch.setattr(ec2_setup, "command", lambda *a, **kw: "")
    original_write = ec2_setup.atomic_write

    def write(path, content, mode=0o600):
        original_write(installed if str(path) == "/etc/nginx/nginx.conf" else path, content, mode)

    monkeypatch.setattr(ec2_setup, "atomic_write", write)
    ec2_setup.DockerHost().proxy(None, tmp_path)
    return installed


def test_proxy_upgrade_validates_before_reload_and_preserves_original(
    d, tmp_path, monkeypatch, managed_proxy
):
    original = managed_proxy.read_text()
    calls = []

    def command(args, **kwargs):
        if args[0] == "nginx" and not calls:
            assert managed_proxy.read_text() == original
        calls.append(args)
        return ""

    monkeypatch.setattr(d, "command", command)
    monkeypatch.setattr(d, "probe_maintenance_proxy", lambda: calls.append(["probe"]))
    d.prepare_maintenance_proxy(tmp_path, managed_proxy)
    assert (tmp_path / "nginx.before-maintenance.conf").read_text() == original
    assert calls[0][:2] == ["nginx", "-t"]
    assert calls[-2:] == [["systemctl", "reload", "nginx"], ["probe"]]
    upgraded = managed_proxy.read_text()
    assert upgraded != original
    # Retry after a crash between writing config and reloading must reload again.
    d.prepare_maintenance_proxy(tmp_path, managed_proxy)
    assert managed_proxy.read_text() == upgraded
    assert (tmp_path / "nginx.before-maintenance.conf").read_text() == original


@pytest.mark.parametrize("failure", ["custom", "invalid", "reload", "readiness"])
def test_proxy_upgrade_failure_preserves_working_configuration(
    d, tmp_path, monkeypatch, managed_proxy, failure
):
    if failure == "custom":
        managed_proxy.write_text(
            managed_proxy.read_text().replace("location /", "location /private")
        )
    original = managed_proxy.read_text()

    def command(args, **kwargs):
        if args[0] == "nginx" and failure == "invalid":
            raise RuntimeError("bad syntax")
        if args[0] == "systemctl" and failure == "reload":
            raise RuntimeError("reload failed")
        return ""

    monkeypatch.setattr(d, "command", command)
    def probe():
        if failure == "readiness":
            raise ValueError("proxy did not become ready")
    monkeypatch.setattr(d, "probe_maintenance_proxy", probe)
    with pytest.raises((ValueError, RuntimeError)):
        d.prepare_maintenance_proxy(tmp_path, managed_proxy)
    assert managed_proxy.read_text() == original


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
        self.ready = True

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

    def applied_live(self):
        return False

    def serving_image(self):
        return self.running

    def recover_cleanup(self, candidate):
        self.events.append("cleanup")

    def compose(self, release):
        return json.dumps(
            {
                "services": {
                    "explorer": {
                        "image": release["image"],
                        "entrypoint": ["mel-explorer", "--no-live"],
                    }
                }
            }
        )

    def boot_compose(self, release):
        return self.compose(release)

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
    monkeypatch.setattr(runtime, "stop", lambda: None)
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
    assert triggers == {
        "pull_request": None,
        "push": {"branches": ["main"]},
        "workflow_dispatch": None,
    }
    assert workflow["concurrency"]["cancel-in-progress"] is False
    release = workflow["jobs"]["release"]
    assert release["permissions"]["id-token"] == "write"
    assert "id-token" not in workflow.get("permissions", {})
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


def workflow_expression(expression, event, ref):
    expression = expression.removeprefix("${{").removesuffix("}}")
    expression = expression.replace("&&", "and").replace("||", "or").strip()
    return eval(
        expression,
        {"__builtins__": {}},
        {"github": SimpleNamespace(event_name=event, ref=ref), "format": str.format},
    )


@pytest.mark.parametrize(
    "event,ref,admitted",
    [
        ("push", "refs/heads/main", True),
        ("workflow_dispatch", "refs/heads/main", True),
        ("workflow_dispatch", "refs/heads/feature", False),
        ("workflow_dispatch", "refs/tags/main", False),
        ("push", "refs/heads/feature", False),
        ("pull_request", "refs/pull/12/merge", False),
        ("pull_request", "refs/heads/main", False),
    ],
)
def test_main_release_artifacts_and_serialization_share_admission(event, ref, admitted):
    workflow = yaml.safe_load(Path(".github/workflows/main.yml").read_text())
    save, upload = workflow["jobs"]["check"]["steps"][-2:]
    assert "docker save" in save["run"]
    assert upload["uses"].startswith("actions/upload-artifact@")
    for gate in (workflow["jobs"]["release"]["if"], save["if"], upload["if"]):
        assert workflow_expression(gate, event, ref) is admitted
    group = workflow_expression(workflow["concurrency"]["group"], event, ref)
    assert (group == "fieldecho-main") is admitted


@pytest.fixture
def voice_workflow():
    workflow = yaml.safe_load(Path(".github/workflows/voice-audio.yml").read_text())
    record = workflow["jobs"]["record"]
    assert record["if"] == "github.ref == 'refs/heads/main'"
    assert record["steps"][0]["with"]["ref"] == "main"
    return workflow


def test_voice_dispatch_has_only_the_required_token_permissions(voice_workflow):
    assert voice_workflow["permissions"] == {
        "contents": "write",
        "pages": "write",
        "actions": "write",
    }
    steps = voice_workflow["jobs"]["record"]["steps"]
    dispatch = next(step for step in steps if step.get("name") == "Request main delivery")
    assert "if" not in dispatch
    assert dispatch["env"] == {"GH_TOKEN": "${{ github.token }}"}
    assert steps.index(dispatch) > next(
        i for i, step in enumerate(steps) if step.get("name") == "Commit the clips"
    )


@pytest.mark.parametrize("scenario", ["publish", "retry", "unchanged", "push-failure"])
def test_voice_clip_tasks_reconcile_main_delivery(voice_workflow, tmp_path, scenario):
    def git(*args, cwd=tmp_path):
        return subprocess.run(
            ["git", *args], cwd=cwd, check=True, text=True, capture_output=True, timeout=10
        ).stdout.strip()

    origin, checkout = tmp_path / "origin.git", tmp_path / "checkout"
    git("init", "--bare", "--initial-branch=main", str(origin))
    git("clone", str(origin), str(checkout))
    git("config", "user.name", "Workflow test", cwd=checkout)
    git("config", "user.email", "workflow@example.invalid", cwd=checkout)
    clips = checkout / "docs/voice/audio"
    clips.mkdir(parents=True)
    (clips / "original.mp3").write_bytes(b"original")
    git("add", ".", cwd=checkout)
    git("commit", "-m", "Initial main", cwd=checkout)
    git("push", "origin", "main", cwd=checkout)
    initial = git("rev-parse", "HEAD", cwd=checkout)
    if scenario != "unchanged":
        (clips / "missing.mp3").write_bytes(b"new clip")
    if scenario == "push-failure":
        hook = origin / "hooks/pre-receive"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o700)
    fake_bin, calls = tmp_path / "bin", tmp_path / "gh-calls"
    fake_bin.mkdir()
    gh = fake_bin / "gh"
    gh.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$GH_CALLS"\nexit "${GH_EXIT_CODE:-0}"\n')
    gh.chmod(0o700)
    env = {
        **os.environ,
        "PATH": str(fake_bin) + os.pathsep + os.environ["PATH"],
        "GH_CALLS": str(calls),
        "GITHUB_SHA": initial,
        "GH_TOKEN": "local-fake-token",
    }
    steps = voice_workflow["jobs"]["record"]["steps"]
    commit = next(i for i, step in enumerate(steps) if step.get("name") == "Commit the clips")
    dispatch = next(
        i for i, step in enumerate(steps) if step.get("name") == "Request main delivery"
    )

    def run_tasks(exit_code="0"):
        for step in steps[commit : dispatch + 1]:
            result = subprocess.run(
                ["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", step["run"]],
                cwd=checkout,
                env={**env, "GH_EXIT_CODE": exit_code},
                text=True,
                capture_output=True,
                timeout=10,
            )
            if result.returncode:
                break
        return result

    result = run_tasks("1" if scenario == "retry" else "0")
    published = git("rev-parse", "refs/heads/main", cwd=origin)
    if scenario in {"unchanged", "push-failure"}:
        assert published == initial
        assert not calls.exists()
        assert (result.returncode == 0) is (scenario == "unchanged")
        return
    assert published != initial
    assert (result.returncode == 0) is (scenario == "publish")
    assert calls.read_text().splitlines() == ["workflow run main.yml --ref main"]
    if scenario == "retry":
        checkout = tmp_path / "rerun"
        git("clone", "--branch", "main", str(origin), str(checkout))
        assert run_tasks().returncode == 0
        assert git("rev-parse", "refs/heads/main", cwd=origin) == published
        assert calls.read_text().splitlines() == ["workflow run main.yml --ref main"] * 2


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


@pytest.mark.parametrize("failure", ["healthy", "candidate", "boot"])
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


def test_actual_healthy_compose_is_proved_before_migration_and_original_is_private(d, target, host):
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
def test_incompatible_host_layout_fails_before_image_probe(
    d, target, tmp_path, monkeypatch, change
):
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
    monkeypatch.setattr(
        runtime,
        "inspect",
        lambda *a: {"Config": {"Labels": {"org.opencontainers.image.revision": SHA}}},
    )

    def config(*a, **k):
        environment = {"MEL_DATABASE_HOST": "127.0.0.1"}
        if "file" not in k:
            environment.update(change.get("environment", {}))
        return json.dumps({"services": {"explorer": {"environment": environment}}})

    monkeypatch.setattr(runtime, "compose_command", config)
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
    assert (
        json.loads((h.root / "compose.yaml").read_text())["services"]["explorer"]["image"]
        == OLD_IMAGE
    )


def test_failed_timeout_recovery_keeps_intent_and_fence(d, target, host, monkeypatch):
    h, r = host
    clock = [100.0]
    monkeypatch.setattr(d.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(d, "HOST_DEADLINE", d.ActivationDeadline(110.0, 130.0))
    release = d.Release(SHA, target.registry + "@" + DIGEST, 15)

    def timeout_after_stop(*args):
        r.running = None
        raise d.ActivationInterrupted("deadline")

    def no_recovery(*args):
        clock[0] = 131.0
        raise d.ActivationInterrupted("recovery deadline")

    r.rehearse_and_migrate = timeout_after_stop
    r.recover_cleanup = no_recovery
    with pytest.raises(d.ActivationInterrupted):
        h.activate(release)
    assert h.read().transition is not None
    assert h.read().highest_sequence == 15
    assert h.read().highest_release == vars(release)
    with pytest.raises(d.ActivationInterrupted):
        h.activate(d.Release("c" * 40, release.image, 16))
    assert h.read().highest_sequence == 15


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


def test_ssm_starts_deadline_before_lock_and_pull_without_repointing_status():
    template = yaml.load(Path("deploy/bootstrap.yaml").read_text(), Loader=yaml.BaseLoader)
    document = template["Resources"]["DeployDocument"]["Properties"]["Content"]
    shell = document["mainSteps"][0]["inputs"]["runCommand"][0]
    assert shell.index("FIELDECHO_ACTIVATE_BY=") < shell.index("flock")
    assert shell.index("FIELDECHO_RECOVER_BY=") < shell.index("docker pull")
    assert "bounded docker pull" in shell
    assert "bounded docker cp" in shell
    assert "bounded aws ecr get-login-password" in shell
    assert "ln -sfn" not in shell
    assert "delivery/current/handler.py host-status" in shell


def test_real_compose_accepts_supported_host_and_rejects_bridge_before_image_run(
    d, target, tmp_path, monkeypatch
):
    import shutil
    import subprocess

    if not shutil.which("docker"):
        pytest.skip("Docker Compose parser unavailable")
    version = subprocess.run(["docker", "compose", "version"], capture_output=True)
    if version.returncode:
        pytest.skip("Docker Compose parser unavailable")
    (tmp_path / "explorer.env").write_text(
        "MEL_DATABASE_HOST=127.0.0.1\nMEL_DATABASE_PORT=55437\nMEL_DATABASE_SSLMODE=disable\n"
    )
    (tmp_path / "database.env").write_text("POSTGRES_USER=app\nPOSTGRES_DB=app\n")
    compose = {
        "services": {
            "postgres": {"image": "postgres:16", "env_file": "database.env"},
            "explorer": {
                "image": OLD_IMAGE,
                "entrypoint": ["mel-explorer", "--allow-live"],
                "command": ["--host", "127.0.0.1", "--port", "8877", "--allow-live"],
                "network_mode": "host",
                "env_file": "explorer.env",
                "environment": {"MEL_DATABASE_HOST": "127.0.0.1", "MEL_DATABASE_PORT": "55437"},
            },
        }
    }
    (tmp_path / "compose.yaml").write_text(json.dumps(compose))
    runtime = d.DockerHost(tmp_path, target)
    monkeypatch.setattr(d, "prepare_maintenance_proxy", lambda root: None)
    monkeypatch.setattr(runtime, "worker_present", lambda: False)
    monkeypatch.setattr(
        runtime,
        "inspect",
        lambda *a: {"Config": {"Labels": {"org.opencontainers.image.revision": SHA}}},
    )
    image_runs = []
    monkeypatch.setattr(runtime, "run_image", lambda *a: image_runs.append(a))
    release = {"sha": SHA, "image": target.registry + "@" + DIGEST, "sequence": 17}
    runtime.preflight(release)
    assert len(image_runs) == 2
    boot = runtime.boot_compose({"image": OLD_IMAGE})
    runtime.validate_compose(boot, "boot")
    parsed_boot = json.loads(
        runtime.compose_command("config", "--format", "json", file="compose.boot.json")
    )["services"]["explorer"]
    assert parsed_boot["image"] == OLD_IMAGE
    assert parsed_boot["entrypoint"] == ["mel-explorer", "--no-live"]
    assert parsed_boot["command"] == ["--host", "127.0.0.1", "--port", "8877"]
    for name, image in (("healthy", OLD_IMAGE), ("candidate", release["image"])):
        runtime.validate_compose(runtime.compose({"image": image}), name)
    compose["services"]["explorer"].pop("network_mode")
    compose["services"]["explorer"]["environment"]["MEL_DATABASE_HOST"] = "postgres"
    (tmp_path / "compose.yaml").write_text(json.dumps(compose))
    bridge = d.DockerHost(tmp_path, target)
    monkeypatch.setattr(bridge, "worker_present", lambda: False)
    monkeypatch.setattr(bridge, "inspect", runtime.inspect)
    monkeypatch.setattr(bridge, "run_image", lambda *a: pytest.fail("bridge image ran"))
    with pytest.raises(ValueError, match="host networking"):
        bridge.preflight(release)


@pytest.mark.parametrize("interruption", ["deadline", "termination"])
def test_real_subprocess_interruption_after_stop_recovers_without_new_request(
    d, target, host, monkeypatch, interruption
):
    import signal
    import sys
    import time

    h, r = host
    previous = signal.signal(signal.SIGALRM, d.interrupt_activation)
    previous_term = signal.signal(signal.SIGTERM, d.interrupt_activation)
    now = time.monotonic()
    monkeypatch.setattr(d, "HOST_DEADLINE", d.ActivationDeadline(now + 0.1, now + 5))

    def blocked_after_stop(*args):
        r.running = None
        script = "import time; time.sleep(20)"
        if interruption == "termination":
            script = (
                "import os, signal, time; os.kill(os.getppid(), signal.SIGTERM); time.sleep(20)"
            )
        d.command([sys.executable, "-c", script])

    r.rehearse_and_migrate = blocked_after_stop
    release = d.Release(SHA, target.registry + "@" + DIGEST, 18)
    try:
        assert h.activate(release)["outcome"] == "FAILED_BEFORE_SWITCH"
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
        signal.signal(signal.SIGTERM, previous_term)
    assert r.running == OLD_IMAGE
    assert h.read().transition is None
    assert h.read().highest_sequence == 18


def test_readiness_propagates_deadline_instead_of_retrying_past_recovery_reserve(
    d, target, tmp_path, monkeypatch
):
    runtime = d.DockerHost.__new__(d.DockerHost)
    runtime.root, runtime.target, runtime.container = tmp_path, target, "explorer"
    monkeypatch.setattr(runtime, "inspect", lambda *a: {"Id": OLD_IMAGE, "Image": OLD_IMAGE})
    monkeypatch.setattr(d.time, "sleep", lambda *a: pytest.fail("deadline swallowed by readiness"))

    def expired(*a, **kw):
        raise d.ActivationInterrupted("activation deadline")

    monkeypatch.setattr(d, "command", expired)
    with pytest.raises(d.ActivationInterrupted):
        runtime.smoke({"image": OLD_IMAGE})


def test_failed_previous_compose_readiness_never_migrates(d, target, host):
    h, r = host
    original = (h.root / "compose.yaml").read_text()

    def unhealthy_previous(*args):
        raise ValueError("previous rendered Compose is not ready")

    r.smoke = unhealthy_previous
    with pytest.raises(ValueError):
        h.activate(d.Release(SHA, target.registry + "@" + DIGEST, 19))
    assert "snapshot" not in r.events
    assert h.read().transition is not None
    assert (h.root / "compose.original.yaml").read_text() == original


def test_expired_startup_command_substitution_never_runs_unbounded(tmp_path):
    import os
    import shutil
    import subprocess

    if not shutil.which("timeout"):
        pytest.skip("coreutils timeout unavailable")
    template = yaml.load(Path("deploy/bootstrap.yaml").read_text(), Loader=yaml.BaseLoader)
    document = template["Resources"]["DeployDocument"]["Properties"]["Content"]
    shell = document["mainSteps"][0]["inputs"]["runCommand"][0]
    prefix = shell.split("cd /opt/dg37", 1)[0]
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    date = fake_bin / "date"
    date.write_text("#!/bin/sh\necho 1000\n")
    date.chmod(0o700)
    marker = tmp_path / "must-not-run"
    script = prefix + '\nFIELDECHO_ACTIVATE_BY=1000\nresult=$(bounded touch "$1")\n'
    result = subprocess.run(
        ["bash", "-c", script, "startup-test", str(marker)],
        env={**os.environ, "PATH": str(fake_bin) + os.pathsep + os.environ["PATH"]},
        capture_output=True,
        timeout=5,
    )
    assert result.returncode != 0
    assert not marker.exists()


class BootRuntime(Runtime):
    def __init__(self, root):
        super().__init__(root)
        self.original = {
            "services": {
                "postgres": {"image": "postgres:16"},
                "explorer": {
                    "image": OLD_IMAGE,
                    "entrypoint": ["mel-explorer", "--host", "127.0.0.1", "--allow-live"],
                    "environment": {"BOOT_CONFIG": "original"},
                },
            }
        }
        self.active = copy.deepcopy(self.original)
        self.old_smokes = 0
        self.failed_old_smokes = set()
        self.fail_original = False
        self.fail_candidate = False
        self.observe_smoke = lambda: None

    def compose(self, release):
        result = copy.deepcopy(self.original)
        app = result["services"]["explorer"]
        app["image"] = release["image"]
        app["entrypoint"] = ["mel-explorer", "--no-live"]
        app["environment"]["BOOT_CONFIG"] = "rendered"
        return json.dumps(result)

    def boot_compose(self, release):
        result = copy.deepcopy(self.original)
        app = result["services"]["explorer"]
        app["image"] = release["image"]
        app["entrypoint"].remove("--allow-live")
        app["entrypoint"].append("--no-live")
        return json.dumps(result)

    def switch(self, compose):
        self.active = json.loads(compose)
        super().switch(compose)

    def smoke(self, release):
        self.observe_smoke()
        app = self.active["services"]["explorer"]
        args = app["entrypoint"] + app.get("command", [])
        assert args.count("--no-live") == 1
        assert "--allow-live" not in args
        if release["image"] == OLD_IMAGE:
            self.old_smokes += 1
            if self.old_smokes in self.failed_old_smokes:
                raise ValueError("rendered previous is unhealthy")
            if self.fail_original and app["environment"]["BOOT_CONFIG"] == "original":
                raise ValueError("original offline fallback is unhealthy")
        elif self.fail_candidate:
            raise ValueError("candidate unhealthy")
        super().smoke(release)


@pytest.fixture
def boot_host(tmp_path, d, target):
    runtime = BootRuntime(tmp_path)
    (tmp_path / "compose.yaml").write_text(json.dumps(runtime.original))
    return d.Host(tmp_path, target, runtime), runtime


def test_initial_previous_failure_uses_original_offline_before_boot_promotion(d, target, boot_host):
    h, r = boot_host
    original = (h.root / "compose.yaml").read_text()
    observed_boot = []
    r.observe_smoke = lambda: observed_boot.append((h.root / "compose.yaml").read_text())
    r.failed_old_smokes = {1, 2}
    result = h.activate(d.Release(SHA, target.registry + "@" + DIGEST, 20))
    assert result["outcome"] == "FAILED_BEFORE_SWITCH"
    assert observed_boot == [original] * 3
    boot = json.loads((h.root / "compose.yaml").read_text())["services"]["explorer"]
    assert boot["environment"] == {"BOOT_CONFIG": "original"}
    assert boot["image"] == OLD_IMAGE
    assert boot["entrypoint"] == ["mel-explorer", "--host", "127.0.0.1", "--no-live"]
    assert "snapshot" not in r.events
    assert h.read().transition is None
    assert h.read().highest_sequence == 20
    assert (h.root / "compose.original.yaml").read_text() == original


def test_failed_recovery_and_fallback_preserve_boot_intent_and_fence(d, target, boot_host):
    h, r = boot_host
    original = (h.root / "compose.yaml").read_text()
    r.failed_old_smokes = {1, 2}
    r.fail_original = True
    release = d.Release(SHA, target.registry + "@" + DIGEST, 21)
    with pytest.raises(ValueError):
        h.activate(release)
    assert (h.root / "compose.yaml").read_text() == original
    state = h.read()
    assert state.transition is not None
    fallback = json.loads(state.transition["boot_compose"])["services"]["explorer"]
    assert fallback["image"] == OLD_IMAGE
    assert "--allow-live" not in fallback["entrypoint"]
    assert "--no-live" in fallback["entrypoint"]
    assert state.highest_sequence == 21
    assert state.highest_release == vars(release)
    assert r.old_smokes == 3
    r.fail_original = False
    h = d.Host(h.root, target, r)
    assert h.activate(d.Release(SHA, release.image, 20))["outcome"] == "REFUSED_STALE"
    assert h.read().transition is None
    assert r.running == OLD_IMAGE


def test_failed_postmigration_rollback_gets_one_proven_boot_fallback(d, target, boot_host):
    h, r = boot_host
    release = d.Release(SHA, target.registry + "@" + DIGEST, 22)
    r.fail_candidate = True
    r.failed_old_smokes = {2}
    result = h.activate(release)
    assert result["outcome"] == "FAILED_ROLLED_BACK"
    assert "migrate-production" in r.events
    assert r.old_smokes == 3
    assert r.running == OLD_IMAGE
    boot = json.loads((h.root / "compose.yaml").read_text())["services"]["explorer"]
    assert boot["environment"] == {"BOOT_CONFIG": "rendered"}
    assert boot["entrypoint"] == ["mel-explorer", "--no-live"]
    assert h.read().transition is None
    assert h.read().highest_sequence == 22


def test_recovery_after_candidate_boot_write_uses_persisted_previous_snapshot(
    d, target, boot_host
):
    h, r = boot_host

    class Crash(BaseException):
        pass

    def crash_after_boot(name):
        if name == "compose":
            raise Crash()

    h.checkpoint = crash_after_boot
    release = d.Release(SHA, target.registry + "@" + DIGEST, 23)
    with pytest.raises(Crash):
        h.activate(release)
    candidate_boot = (h.root / "compose.yaml").read_text()
    assert json.loads(candidate_boot)["services"]["explorer"]["image"] == release.image
    r.failed_old_smokes = {2}
    observed_boot = []
    r.observe_smoke = lambda: observed_boot.append((h.root / "compose.yaml").read_text())
    h = d.Host(h.root, target, r)
    assert h.activate(d.Release(SHA, release.image, 22))["outcome"] == "REFUSED_STALE"
    assert observed_boot == [candidate_boot, candidate_boot]
    assert r.old_smokes == 3
    assert r.running == OLD_IMAGE
    assert h.read().deployed["sha"] == OLD
    assert h.read().transition is None
    boot = json.loads((h.root / "compose.yaml").read_text())
    assert boot["services"]["explorer"]["image"] == OLD_IMAGE


@pytest.mark.parametrize("expired", [False, True])
def test_fallback_uses_only_remaining_absolute_recovery_budget(
    d, target, boot_host, monkeypatch, expired
):
    h, r = boot_host
    clock = [100.0]
    monkeypatch.setattr(d.time, "monotonic", lambda: clock[0])
    budget = d.ActivationDeadline(110.0, 130.0)
    monkeypatch.setattr(d, "HOST_DEADLINE", budget)
    release = d.Release(SHA, target.registry + "@" + DIGEST, 24)
    r.failed_old_smokes = {1, 2}
    switches = []
    switch = r.switch

    def bounded_switch(compose):
        switches.append(budget.remaining())
        switch(compose)

    def advance_recovery_clock():
        if r.old_smokes == 1:
            clock[0] = 131.0 if expired else 125.0

    r.switch = bounded_switch
    r.observe_smoke = advance_recovery_clock
    try:
        if expired:
            with pytest.raises(d.ActivationInterrupted):
                h.activate(release)
            assert len(switches) == 2
            assert h.read().transition is not None
        else:
            assert h.activate(release)["outcome"] == "FAILED_BEFORE_SWITCH"
            assert switches == [10.0, 15.0, 5.0]
            assert h.read().transition is None
        assert budget.recover_by == 130.0
        assert h.read().highest_sequence == 24
    finally:
        d.signal.setitimer(d.signal.ITIMER_REAL, 0)


def test_recovery_state_write_failure_keeps_in_memory_and_persisted_intent(d, target, boot_host):
    h, r = boot_host

    class Crash(BaseException):
        pass

    h.checkpoint = lambda name: (_ for _ in ()).throw(Crash())
    release = d.Release(SHA, target.registry + "@" + DIGEST, 25)
    with pytest.raises(Crash):
        h.activate(release)
    state = h.read()
    save = h.save

    def fail_recovery_commit(recovered):
        if recovered.transition is None:
            raise OSError("state commit failed")
        save(recovered)

    h.save = fail_recovery_commit
    with pytest.raises(OSError):
        h.recover(state)
    assert state.transition is not None
    assert h.read().transition is not None
    h.save = save
    assert h.recover(h.read()).transition is None
    assert h.read().highest_sequence == 25


@pytest.mark.parametrize("shell", [False, True])
def test_boot_snapshot_pins_image_and_disables_all_live_flags(
    d, target, tmp_path, monkeypatch, shell
):
    runtime = d.DockerHost.__new__(d.DockerHost)
    runtime.root, runtime.target = tmp_path, target
    original = {
        "services": {
            "postgres": {"image": "postgres:16"},
            "explorer": {
                "build": {"context": "."},
                "image": "mutable-tag",
                "entrypoint": ["mel-explorer", "--allow-live", "--no-live"],
                "command": ["--host", "127.0.0.1", "--allow-live"],
                "environment": {"MEL_DATABASE_HOST": "127.0.0.1"},
                "volumes": ["/data:/data:ro"],
            },
        }
    }
    runtime.current = copy.deepcopy(original)
    monkeypatch.setattr(runtime, "compose_command", lambda *a: json.dumps(runtime.current))
    if shell:
        runtime.current["services"]["explorer"]["entrypoint"] = "sh -c 'mel-explorer --allow-live'"
        with pytest.raises(ValueError):
            runtime.boot_compose({"image": OLD_IMAGE})
        return
    result = json.loads(runtime.boot_compose({"image": OLD_IMAGE}))
    app = result["services"]["explorer"]
    assert app["entrypoint"] == ["mel-explorer", "--no-live"]
    assert app["command"] == ["--host", "127.0.0.1"]
    assert app["image"] == OLD_IMAGE
    assert "build" not in app
    assert app["environment"] == original["services"]["explorer"]["environment"]
    assert app["volumes"] == original["services"]["explorer"]["volumes"]
    assert result["services"]["postgres"] == original["services"]["postgres"]
    assert runtime.current == original


@pytest.mark.parametrize("window", ["intent", "compose"])
def test_legacy_pending_recovery_never_falls_back_to_disk_or_original(d, target, boot_host, window):
    h, r = boot_host

    class Crash(BaseException):
        pass

    def crash(name):
        if name == window:
            raise Crash()

    h.checkpoint = crash
    release = d.Release(SHA, target.registry + "@" + DIGEST, 26)
    with pytest.raises(Crash):
        h.activate(release)
    raw = json.loads(h.state_path.read_text())
    raw["transition"].pop("boot_compose", None)
    h.state_path.write_text(json.dumps(raw))
    boot = (h.root / "compose.yaml").read_text()
    r.failed_old_smokes = {r.old_smokes + 1}
    with pytest.raises(ValueError):
        h.activate(release)
    assert (h.root / "compose.yaml").read_text() == boot
    assert h.read().transition is not None
    assert h.read().highest_sequence == 26
    r.failed_old_smokes.clear()
    assert h.activate(d.Release(SHA, release.image, 25))["outcome"] == "REFUSED_STALE"
    assert h.read().transition is None


def test_real_primary_recovery_timeout_leaves_budget_for_offline_fallback(
    d, target, boot_host, monkeypatch
):
    import signal
    import sys
    import time

    h, r = boot_host

    class Crash(BaseException):
        pass

    h.checkpoint = lambda name: (_ for _ in ()).throw(Crash())
    release = d.Release(SHA, target.registry + "@" + DIGEST, 27)
    with pytest.raises(Crash):
        h.activate(release)
    smoke = r.smoke
    original_boot = (h.root / "compose.yaml").read_text()
    attempts = []

    def blocked_rendered(release):
        assert (h.root / "compose.yaml").read_text() == original_boot
        mode = r.active["services"]["explorer"]["environment"]["BOOT_CONFIG"]
        attempts.append(mode)
        if mode == "rendered":
            d.command([sys.executable, "-c", "import time; time.sleep(20)"])
        else:
            assert budget.remaining() > 0
            smoke(release)

    r.smoke = blocked_rendered
    now = time.monotonic()
    budget = d.ActivationDeadline(now + 0.1, now + 2)
    monkeypatch.setattr(d, "HOST_DEADLINE", budget)
    previous = signal.signal(signal.SIGALRM, d.interrupt_activation)
    try:
        assert h.recover(h.read()).transition is None
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
    assert attempts == ["rendered", "original"]
    assert budget.recover_by == now + 2
    assert r.running == OLD_IMAGE
    assert h.read().transition is None


@pytest.mark.parametrize("argument", ["--allow", "--allow-l", "--allow-live=true", "--no"])
def test_ambiguous_boot_live_arguments_are_rejected(d, target, tmp_path, monkeypatch, argument):
    runtime = d.DockerHost.__new__(d.DockerHost)
    runtime.root, runtime.target = tmp_path, target
    runtime.current = {
        "services": {
            "explorer": {"entrypoint": ["mel-explorer"], "command": [argument], "image": OLD_IMAGE}
        }
    }
    monkeypatch.setattr(runtime, "compose_command", lambda *a: json.dumps(runtime.current))
    with pytest.raises(ValueError):
        runtime.boot_compose({"image": OLD_IMAGE})


def test_boot_snapshot_uses_current_disk_after_pending_recovery(d, target, tmp_path, monkeypatch):
    runtime = d.DockerHost.__new__(d.DockerHost)
    runtime.root, runtime.target = tmp_path, target
    original = {
        "services": {
            "explorer": {
                "image": OLD_IMAGE,
                "entrypoint": ["mel-explorer", "--no-live"],
                "environment": {"BOOT_CONFIG": "proven-original"},
            }
        }
    }
    runtime.current = copy.deepcopy(original)
    runtime.current["services"]["explorer"]["environment"]["BOOT_CONFIG"] = "cached-candidate"
    calls = []

    def config(*args):
        calls.append(args)
        return json.dumps(original)

    monkeypatch.setattr(runtime, "compose_command", config)
    actual = json.loads(runtime.boot_compose({"image": OLD_IMAGE}))
    assert actual == original
    assert calls == [("config", "--no-env-resolution", "--format", "json")]


@pytest.mark.parametrize("corruption", ["image", "live", "shell"])
def test_invalid_persisted_boot_snapshot_blocks_recovery_before_switch(
    d, target, boot_host, corruption
):
    h, r = boot_host

    class Crash(BaseException):
        pass

    h.checkpoint = lambda name: (_ for _ in ()).throw(Crash())
    release = d.Release(SHA, target.registry + "@" + DIGEST, 28)
    with pytest.raises(Crash):
        h.activate(release)
    raw = json.loads(h.state_path.read_text())
    boot = json.loads(raw["transition"]["boot_compose"])
    app = boot["services"]["explorer"]
    if corruption == "image":
        app["image"] = release.image
    elif corruption == "live":
        app["command"] = ["--allow-live"]
    else:
        app["entrypoint"] = "sh -c 'mel-explorer --no-live'"
    raw["transition"]["boot_compose"] = json.dumps(boot)
    h.state_path.write_text(json.dumps(raw))
    original = (h.root / "compose.yaml").read_text()
    r.events.clear()
    with pytest.raises(ValueError):
        h.activate(release)
    assert not r.events
    assert (h.root / "compose.yaml").read_text() == original
    assert json.loads(h.state_path.read_text()) == raw


def test_cleanup_failure_starts_offline_fallback_but_keeps_pending_intent(d, target, boot_host):
    h, r = boot_host
    original = (h.root / "compose.yaml").read_text()
    r.failed_old_smokes = {1}

    def failed_cleanup(candidate):
        raise ValueError("cleanup failed")

    r.recover_cleanup = failed_cleanup
    release = d.Release(SHA, target.registry + "@" + DIGEST, 29)
    with pytest.raises(ValueError, match="cleanup failed"):
        h.activate(release)
    assert r.active["services"]["explorer"]["environment"] == {"BOOT_CONFIG": "original"}
    assert r.old_smokes == 2
    assert h.read().transition is not None
    assert h.read().highest_sequence == 29
    assert (h.root / "compose.yaml").read_text() == original


def test_candidate_state_commit_failure_and_failed_render_recover_from_boot_snapshot(
    d, target, boot_host
):
    h, r = boot_host
    save = h.save
    release = d.Release(SHA, target.registry + "@" + DIGEST, 30)
    r.failed_old_smokes = {2}
    observed_boot = []
    r.observe_smoke = lambda: observed_boot.append((h.root / "compose.yaml").read_text())

    def fail_candidate_commit(state):
        if state.last_outcome == "DEPLOYED":
            raise OSError("candidate state commit failed")
        save(state)

    h.save = fail_candidate_commit
    result = h.activate(release)
    assert result["outcome"] == "FAILED_ROLLED_BACK"
    assert r.old_smokes == 3
    assert all(
        json.loads(compose)["services"]["explorer"]["image"] == release.image
        for compose in observed_boot[-2:]
    )
    assert r.running == OLD_IMAGE
    assert h.read().deployed["sha"] == OLD
    assert h.read().transition is None
    assert h.read().highest_sequence == 30
    assert (h.root / "COMMIT").read_text() == OLD + "\n"


@pytest.mark.parametrize("value", [None, True, False])
def test_root_host_policy_defaults_offline_and_strict_boolean(d, tmp_path, value):
    if value is not None:
        d.atomic_write(tmp_path / "live-policy.json", json.dumps(value))
    assert d.read_live_policy(tmp_path) is bool(value)


@pytest.mark.parametrize("raw", ['"true"', "{}", '{"live":true}', "1", "null", "true false"])
def test_malformed_host_policy_refuses(d, tmp_path, raw):
    (tmp_path / "live-policy.json").write_text(raw)
    with pytest.raises(ValueError):
        d.read_live_policy(tmp_path)


def test_host_policy_refuses_symlink_or_writable_policy(d, tmp_path):
    policy = tmp_path / "live-policy.json"
    target = tmp_path / "other.json"
    target.write_text("true")
    policy.symlink_to(target)
    with pytest.raises(ValueError):
        d.read_live_policy(tmp_path)
    policy.unlink()
    policy.write_text("true")
    policy.chmod(0o666)
    with pytest.raises(ValueError):
        d.read_live_policy(tmp_path)


def test_candidate_render_uses_explicit_policy_and_offline_normalization_strips_runner(d):
    current = {
        "services": {
            "postgres": {},
            "explorer": {
                "entrypoint": ["mel-explorer", "--run-jobs"],
                "command": ["--run-jobs", "--no-live"],
            },
        }
    }
    live = d.render_compose(current, OLD_IMAGE, live=True)["services"]["explorer"]
    assert "--run-jobs" in live["entrypoint"] and "--no-live" not in live["entrypoint"]
    offline = d.offline_boot_compose(current, OLD_IMAGE)["services"]["explorer"]
    assert offline["entrypoint"] == ["mel-explorer", "--no-live"]
    assert offline["command"] == []


def test_readiness_requires_owned_runner_for_live(d):
    proof = {
        "database": "connected",
        "live_enabled": True,
        "auth_required": True,
        "runner_ready": True,
    }
    d.check_readiness(proof, 401, {}, [200, 200], False, live=True)
    for change in ({"runner_ready": False}, {"auth_required": False}, {"live_enabled": False}):
        with pytest.raises(ValueError):
            d.check_readiness({**proof, **change}, 401, {}, [200, 200], False, live=True)


class LiveRuntime(Runtime):
    def __init__(self, root):
        super().__init__(root)
        self.live = False
        self.ready = True
        self.backlog = []

    def applied_live(self):
        return self.live

    def compose(self, release, live=False):
        result = json.loads(super().compose(release))
        if live:
            result["services"]["explorer"]["entrypoint"] = ["mel-explorer", "--run-jobs"]
        return json.dumps(result)

    def switch(self, compose):
        self.events.append("stop")
        self.live = "--run-jobs" in json.loads(compose)["services"]["explorer"]["entrypoint"]
        super().switch(compose)

    def smoke(self, release, live=False):
        assert self.live == live
        super().smoke(release)
        self.ready = True

    def require_empty_queue(self, release):
        self.events.append("queue-inventory")
        assert not self.live
        if self.backlog:
            raise ValueError("queued or running jobs require operator attention")

    def live_preflight(self, release):
        self.events.append("live-preflight")


@pytest.fixture
def live_host(tmp_path, d, target):
    runtime = LiveRuntime(tmp_path)
    (tmp_path / "compose.yaml").write_text(runtime.compose(runtime.adopt()))
    h = d.Host(tmp_path, target, runtime)
    assert h.activate(d.Release(SHA, target.registry + "@" + DIGEST, 40))["outcome"] == "DEPLOYED"
    runtime.events.clear()
    return h, runtime


def test_same_image_live_enable_disable_uses_journal_and_keeps_fence(d, live_host):
    h, r = live_host
    before = h.read()
    assert h.set_mode(True)["outcome"] == "DEPLOYED"
    assert r.live and d.read_live_policy(h.root)
    after = h.read()
    assert after.deployed == before.deployed
    assert after.previous == before.previous
    assert after.highest_sequence == before.highest_sequence
    assert after.highest_release == before.highest_release
    assert "queue-inventory" in r.events
    assert h.report(after)["desired_mode"] == "live"
    assert h.report(after)["applied_mode"] == "live"
    assert h.set_mode(False)["outcome"] == "DEPLOYED"
    assert not r.live and not d.read_live_policy(h.root)


@pytest.mark.parametrize("status", ["queued", "running"])
def test_initial_enable_refuses_backlog_without_policy_or_row_mutation(d, live_host, status):
    h, r = live_host
    r.backlog = [{"status": status, "job_id": "retained"}]
    original = copy.deepcopy(r.backlog)
    result = h.set_mode(True)
    assert result["outcome"] == "FAILED_BEFORE_SWITCH"
    assert r.backlog == original and not r.live
    assert not d.read_live_policy(h.root)
    assert h.read().transition is None


def test_live_policy_survives_next_release_and_failed_candidate_recovers_offline(d, live_host):
    h, r = live_host
    assert h.set_mode(True)["outcome"] == "DEPLOYED"
    release = d.Release("c" * 40, r.running.rsplit("@", 1)[0] + "@sha256:" + "3" * 64, 41)
    assert h.activate(release)["outcome"] == "DEPLOYED"
    assert r.live
    r.fail = "migration"
    failed = d.Release("d" * 40, release.image, 42)
    assert h.activate(failed)["outcome"] == "FAILED_BEFORE_SWITCH"
    report = h.report(h.read())
    assert report["desired_mode"] == "live" and report["applied_mode"] == "offline"
    assert d.read_live_policy(h.root)
    r.fail = None
    assert h.set_mode(True)["outcome"] == "DEPLOYED"
    assert r.live and h.read().highest_sequence == 42


@pytest.mark.parametrize("window", ["intent", "policy", "switched", "compose"])
def test_mode_crash_recovers_offline_and_preserves_wire_state(d, live_host, window):
    h, r = live_host

    class Crash(BaseException):
        pass

    def crash(name):
        if name == window:
            raise Crash()

    h.checkpoint = crash
    with pytest.raises(Crash):
        h.set_mode(True)
    raw = json.loads(h.state_path.read_text())
    assert set(raw["transition"]) == {"healthy_compose", "candidate", "boot_compose"}
    h = d.Host(h.root, h.target, r)
    state = h.recover(h.read())
    assert state.transition is None and state.highest_sequence == 40
    assert not r.live
    assert d.read_live_policy(h.root) == (window != "intent")
    boot = json.loads((h.root / "compose.yaml").read_text())
    assert "--run-jobs" not in boot["services"]["explorer"]["entrypoint"]


def test_stop_waits_for_managed_container_and_checks_lease_before_replacement(
    d, tmp_path, monkeypatch
):
    runtime = d.DockerHost.__new__(d.DockerHost)
    runtime.root, runtime.container = tmp_path, "managed"
    events = []
    monkeypatch.setattr(runtime, "compose_command", lambda *a, **kw: events.append(a))
    monkeypatch.setattr(
        runtime, "inspect", lambda _: {"Image": OLD_IMAGE, "State": {"Running": False}}
    )
    monkeypatch.setattr(runtime, "worker_present", lambda: events.append("inventory") or False)
    monkeypatch.setattr(runtime, "run_image", lambda image, script: events.append("lease-proof"))
    runtime.switch('{"services":{}}')
    assert events == [
        ("stop", "--timeout", "40", "explorer"),
        "inventory",
        "lease-proof",
        ("up", "-d", "--no-build", "--no-deps", "--force-recreate", "explorer"),
    ]
    assert not runtime.ready


@pytest.mark.parametrize("failure", ["running", "stray", "lease"])
def test_unproven_quiescence_never_replaces_app(d, tmp_path, monkeypatch, failure):
    runtime = d.DockerHost.__new__(d.DockerHost)
    runtime.root, runtime.container = tmp_path, "managed"
    calls = []
    monkeypatch.setattr(runtime, "compose_command", lambda *a, **kw: calls.append(a))
    monkeypatch.setattr(
        runtime,
        "inspect",
        lambda _: {"Image": OLD_IMAGE, "State": {"Running": failure == "running"}},
    )
    monkeypatch.setattr(runtime, "worker_present", lambda: failure == "stray")

    def lease(*args):
        if failure == "lease":
            raise ValueError("lease remains")

    monkeypatch.setattr(runtime, "run_image", lease)
    with pytest.raises(ValueError):
        runtime.switch('{"services":{}}')
    assert len(calls) == 1
    assert not (tmp_path / "compose.activation.json").exists()


@pytest.mark.parametrize(
    "commandline,expected",
    [
        ("mel-worker jobs", True),
        ("python -m mel.worker jobs", True),
        ("mel-explorer --run-jobs", True),
        ("python -m mel.explorer.api --run-jobs", True),
        ("mel-explorer --no-live", False),
        ("python unrelated.py", False),
    ],
)
def test_native_runner_inventory_excludes_only_managed_pid(d, monkeypatch, commandline, expected):
    runtime = d.DockerHost.__new__(d.DockerHost)
    runtime.container = "managed"
    monkeypatch.setattr(runtime, "inspect", lambda _: {"Id": "managed-id", "State": {"Pid": 100}})
    monkeypatch.setattr(
        d,
        "command",
        lambda args: (
            "" if args[0] == "docker" else "100 mel-explorer --run-jobs\n101 " + commandline
        ),
    )
    assert runtime.worker_present() == expected


def test_stray_integrated_container_is_not_mistaken_for_owned_runner(d, monkeypatch):
    runtime = d.DockerHost.__new__(d.DockerHost)
    runtime.container = "managed"
    containers = {
        "managed": {"Id": "managed-id", "State": {"Pid": 100}},
        "managed-id": {"Id": "managed-id", "Path": "mel-explorer", "Args": ["--run-jobs"]},
        "stray-id": {"Id": "stray-id", "Path": "mel-explorer", "Args": ["--run-jobs"]},
    }
    monkeypatch.setattr(runtime, "inspect", lambda name: containers[name])
    monkeypatch.setattr(d, "command", lambda args: "managed-id stray-id")
    assert runtime.worker_present()


def test_recovery_stops_candidate_before_cleanup_and_switch(d, tmp_path, monkeypatch):
    runtime = d.DockerHost.__new__(d.DockerHost)
    runtime.root = tmp_path
    (tmp_path / "database.env").write_text("POSTGRES_USER=mel\n")
    events = []
    monkeypatch.setattr(runtime, "stop", lambda: events.append("stop"))
    monkeypatch.setattr(d, "command", lambda args: events.append("cleanup") or "")
    monkeypatch.setattr(runtime, "pg", lambda *args: events.append("drop-rehearsal"))
    runtime.recover_cleanup({"sequence": 50})
    assert events == ["stop", "cleanup", "drop-rehearsal"]
    assert runtime.sequence == 50
