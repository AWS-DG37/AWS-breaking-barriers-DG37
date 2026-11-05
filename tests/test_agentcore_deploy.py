"""Release safety at the AWS boundary; all file validation uses real artifacts."""

import dataclasses
import hashlib
import json
import zipfile
from pathlib import Path

import pytest

from deploy import agentcore as a

SHA = "a" * 40
ARN = "arn:aws:bedrock-agentcore:us-east-1:047640882913:runtime/dg37_agent-ABCDEFGHIJ"


def write_member(archive, name, data):
    entry = zipfile.ZipInfo(name)
    entry.create_system = 3
    entry.external_attr = 0o100644 << 16
    archive.writestr(entry, data)


@pytest.fixture
def target():
    return a.Target.parse(
        dict(
            account="047640882913",
            region="us-east-1",
            runtime_arn=ARN,
            execution_role_arn="arn:aws:iam::047640882913:role/dg37-agent",
            package_bucket="dg37-agent-047640882913",
            package_prefix="releases",
            github_repository="AWS-DG37/AWS-breaking-barriers-DG37",
        )
    )


@pytest.fixture
def package(tmp_path):
    p = tmp_path / "agent.zip"
    with zipfile.ZipFile(p, "w") as z:
        write_member(z, "main.py", "entrypoint")
        write_member(z, "release.json", json.dumps({"sha": SHA}))
    return p


@pytest.mark.parametrize(
    "field,value",
    [
        ("account", "984989930904"),
        ("region", "us-west-2"),
        ("runtime_arn", ARN.replace("047640882913", "984989930904")),
        ("execution_role_arn", "arn:aws:iam::984989930904:role/foreign"),
        ("package_prefix", "../other"),
        ("package_bucket", ""),
    ],
)
def test_foreign_or_unsafe_target_refuses(target, field, value):
    with pytest.raises(ValueError):
        a.Target.parse({**dataclasses.asdict(target), field: value})


def test_package_rejects_wrong_revision_and_unsafe_members(package):
    with pytest.raises(ValueError, match="revision"):
        a.validate_package(package, "b" * 40)
    with zipfile.ZipFile(package, "a") as z:
        write_member(z, "data/insights.jsonl", "private data")
    with pytest.raises(ValueError, match="state"):
        a.validate_package(package, SHA)


class Cloud:
    def __init__(self, target, package):
        self.target, self.package = target, package
        self.calls = []
        self.current = {
            "agentRuntimeArn": ARN,
            "agentRuntimeVersion": "1",
            "status": "READY",
            "roleArn": target.execution_role_arn,
            "networkConfiguration": {"networkMode": "PUBLIC"},
            "protocolConfiguration": {"serverProtocol": "HTTP"},
            "environmentVariables": {"KEEP": "yes"},
            "agentRuntimeArtifact": {
                "codeConfiguration": {
                    "code": {
                        "s3": {
                            "bucket": target.package_bucket,
                            "prefix": "initial.zip",
                            "versionId": "initial-version",
                        }
                    },
                    "runtime": "PYTHON_3_12",
                    "entryPoint": ["main.py"],
                }
            },
        }
        self.failed = False

    def __call__(self, target, service, operation, **params):
        self.calls.append((service, operation, params))
        if operation == "get-caller-identity":
            return {"Account": target.account}
        if operation == "get-bucket-versioning":
            return {"Status": "Enabled"}
        if operation == "head-object":
            raise RuntimeError("not found")
        if operation == "put-object":
            assert params["if_none_match"] == "*"
            return {"VersionId": "immutable-version"}
        if operation == "update-agent-runtime":
            assert params["environment_variables"] == {"KEEP": "yes"}
            self.current.update(
                agentRuntimeVersion="2", agentRuntimeArtifact=params["agent_runtime_artifact"]
            )
            return {"agentRuntimeVersion": "2"}
        if operation == "get-agent-runtime":
            return {**self.current, "status": "UPDATE_FAILED" if self.failed else "READY"}
        raise AssertionError((service, operation, params))


def test_stale_main_never_mutates(target, package, monkeypatch):
    cloud = Cloud(target, package)
    monkeypatch.setattr(a, "aws", cloud)
    monkeypatch.setattr(a, "main_head", lambda t: "b" * 40)
    assert a.deploy(target, package, SHA)["outcome"] == "SUPERSEDED"
    assert not cloud.calls


def test_guard_rechecks_immediately_before_update(target, package, monkeypatch):
    cloud = Cloud(target, package)
    monkeypatch.setattr(a, "aws", cloud)
    heads = iter([SHA, "b" * 40])
    monkeypatch.setattr(a, "main_head", lambda t: next(heads))
    assert a.deploy(target, package, SHA)["outcome"] == "SUPERSEDED"
    assert not any(op == "update-agent-runtime" for _, op, _ in cloud.calls)


def test_versioned_update_keeps_configuration_and_noops_same_artifact(target, package, monkeypatch):
    cloud = Cloud(target, package)
    monkeypatch.setattr(a, "aws", cloud)
    monkeypatch.setattr(a, "main_head", lambda t: SHA)
    smokes = []
    monkeypatch.setattr(a, "smoke", lambda t, v: smokes.append(v))
    result = a.deploy(target, package, SHA)
    assert result["previous_version"] == "1" and result["version"] == "2"
    code = cloud.current["agentRuntimeArtifact"]["codeConfiguration"]
    assert code["code"]["s3"]["versionId"] == "immutable-version"
    assert (
        code["code"]["s3"]["prefix"]
        == f"releases/{SHA}/{hashlib.sha256(package.read_bytes()).hexdigest()}.zip"
    )
    assert smokes == ["2"]
    cloud.calls.clear()
    assert a.deploy(target, package, SHA)["outcome"] == "ALREADY_DEPLOYED"
    assert not any(op in ("put-object", "update-agent-runtime") for _, op, _ in cloud.calls)


def test_update_failure_is_loud_and_reports_preserved_version(target, package, monkeypatch, capsys):
    cloud = Cloud(target, package)
    call = cloud.__call__

    def aws(*args, **kw):
        value = call(*args, **kw)
        if args[2] == "update-agent-runtime":
            cloud.failed = True
        return value

    monkeypatch.setattr(a, "aws", aws)
    monkeypatch.setattr(a, "main_head", lambda t: SHA)
    with pytest.raises(RuntimeError, match="UPDATE_FAILED"):
        a.deploy(target, package, SHA)
    assert '"previous_version": "1"' in capsys.readouterr().out


def test_smoke_rejects_application_failure(target, monkeypatch):
    def command(args, **kwargs):
        Path(args[-1]).write_text('{"ok": false, "error": "failed"}')
        return "{}"

    monkeypatch.setattr(a, "command", command)
    monkeypatch.setattr(a, "aws", lambda *args, **kw: {"status": "READY", "liveVersion": "2"})
    with pytest.raises(RuntimeError, match="options"):
        a.smoke(target, "2")


def test_smoke_requires_default_endpoint_to_serve_candidate(target, monkeypatch):
    monkeypatch.setattr(a, "aws", lambda *args, **kw: {"status": "READY", "liveVersion": "1"})
    monkeypatch.setattr(
        a, "command", lambda *args, **kw: pytest.fail("must not invoke old version")
    )
    monkeypatch.setattr(a.time, "sleep", lambda _: None)
    with pytest.raises(RuntimeError, match="endpoint"):
        a.smoke(target, "2")


def test_package_builder_is_deterministic_and_excludes_local_state(tmp_path):
    source, deps = tmp_path / "agents", tmp_path / "deps"
    source.mkdir()
    deps.mkdir()
    (source / "main.py").write_text("main")
    (source / "data").mkdir()
    (source / "data" / "insights.jsonl").write_text("secret")
    (deps / "dependency.py").write_text("dependency")
    outputs = [tmp_path / "one.zip", tmp_path / "two.zip"]
    for output in outputs:
        a.build(source, deps, output, SHA)
    assert outputs[0].read_bytes() == outputs[1].read_bytes()
    with zipfile.ZipFile(outputs[0]) as archive:
        assert set(archive.namelist()) == {"main.py", "dependency.py", "release.json"}


def test_dependency_data_is_packaged_without_application_dataset(package):
    with zipfile.ZipFile(package, "a") as archive:
        write_member(archive, "botocore/data/bedrock-agentcore/service-2.json", "{}")
    assert len(a.validate_package(package, SHA)) == 64


@pytest.mark.parametrize("condition", ["foreign-caller", "unversioned-bucket", "missing-version"])
def test_cloud_contract_failure_refuses_runtime_update(target, package, monkeypatch, condition):
    cloud = Cloud(target, package)

    def aws(t, service, operation, **params):
        if condition == "foreign-caller" and operation == "get-caller-identity":
            return {"Account": "984989930904"}
        if condition == "unversioned-bucket" and operation == "get-bucket-versioning":
            return {"Status": "Suspended"}
        if condition == "missing-version" and operation == "put-object":
            return {"VersionId": "null"}
        return cloud(t, service, operation, **params)

    monkeypatch.setattr(a, "aws", aws)
    monkeypatch.setattr(a, "main_head", lambda t: SHA)
    with pytest.raises(ValueError):
        a.deploy(target, package, SHA)
    assert not any(op == "update-agent-runtime" for _, op, _ in cloud.calls)


def test_existing_immutable_object_can_resume_without_overwrite(target, package, monkeypatch):
    import base64

    cloud = Cloud(target, package)
    digest = hashlib.sha256(package.read_bytes()).hexdigest()

    def aws(t, service, operation, **params):
        if operation == "head-object":
            return {
                "Metadata": {"sha": SHA, "sha256": digest},
                "ContentLength": package.stat().st_size,
                "VersionId": "existing-version",
                "ChecksumSHA256": base64.b64encode(bytes.fromhex(digest)).decode(),
            }
        return cloud(t, service, operation, **params)

    monkeypatch.setattr(a, "aws", aws)
    monkeypatch.setattr(a, "main_head", lambda t: SHA)
    monkeypatch.setattr(a, "smoke", lambda *args: None)
    assert a.deploy(target, package, SHA)["s3_version"] == "existing-version"
    assert not any(op == "put-object" for _, op, _ in cloud.calls)


def test_runtime_ready_timeout_fails_without_smoke(target, package, monkeypatch):
    cloud = Cloud(target, package)

    def aws(t, service, operation, **params):
        result = cloud(t, service, operation, **params)
        if operation == "get-agent-runtime" and params.get("agent_runtime_version"):
            return {**result, "status": "UPDATING"}
        return result

    monkeypatch.setattr(a, "aws", aws)
    monkeypatch.setattr(a, "main_head", lambda t: SHA)
    monkeypatch.setattr(a.time, "sleep", lambda _: None)
    monkeypatch.setattr(
        a, "smoke", lambda *args: pytest.fail("unready runtime must not be invoked")
    )
    with pytest.raises(RuntimeError, match="timeout"):
        a.deploy(target, package, SHA)


def test_codezip_files_are_readable_by_runtime_uid(tmp_path):
    source, deps = tmp_path / "agents", tmp_path / "deps"
    source.mkdir()
    deps.mkdir()
    (source / "main.py").write_text("entrypoint")
    (deps / "native.so").write_bytes(b"ELF")
    package = tmp_path / "agent.zip"
    a.build(source, deps, package, SHA)
    with zipfile.ZipFile(package) as archive:
        for entry in archive.infolist():
            mode = entry.external_attr >> 16
            assert entry.create_system == 3
            assert mode & 0o777 == 0o644


def test_package_refuses_runtime_unreadable_file(package):
    with zipfile.ZipFile(package, "a") as archive:
        archive.writestr("unreadable.py", "private mode")
    with pytest.raises(ValueError, match="permissions"):
        a.validate_package(package, SHA)


@pytest.mark.parametrize("same_package", [False, True])
def test_failed_update_can_fix_forward_even_with_same_package(
    target, package, monkeypatch, same_package
):
    cloud = Cloud(target, package)
    cloud.current["status"] = "UPDATE_FAILED"
    if same_package:
        digest = hashlib.sha256(package.read_bytes()).hexdigest()
        cloud.current["agentRuntimeArtifact"]["codeConfiguration"]["code"]["s3"].update(
            prefix=f"releases/{SHA}/{digest}.zip", versionId="failed-version"
        )

    def aws(t, service, operation, **params):
        result = cloud(t, service, operation, **params)
        if operation == "get-agent-runtime" and not params.get("agent_runtime_version"):
            return {**result, "status": "UPDATE_FAILED"}
        return result

    monkeypatch.setattr(a, "aws", aws)
    monkeypatch.setattr(a, "main_head", lambda t: SHA)
    monkeypatch.setattr(a, "smoke", lambda *args: None)
    assert a.deploy(target, package, SHA)["outcome"] == "DEPLOYED"
    assert len([op for _, op, _ in cloud.calls if op == "update-agent-runtime"]) == 1


@pytest.mark.parametrize(
    "status", ["CREATING", "UPDATING", "DELETING", "CREATE_FAILED", "DELETE_FAILED"]
)
def test_runtime_transitions_and_other_failures_refuse_update(target, package, monkeypatch, status):
    cloud = Cloud(target, package)

    def aws(t, service, operation, **params):
        result = cloud(t, service, operation, **params)
        if operation == "get-agent-runtime":
            return {**result, "status": status}
        return result

    monkeypatch.setattr(a, "aws", aws)
    monkeypatch.setattr(a, "main_head", lambda t: SHA)
    with pytest.raises(ValueError):
        a.deploy(target, package, SHA)
    assert not any(op == "update-agent-runtime" for _, op, _ in cloud.calls)


@pytest.mark.parametrize("explicit_arn", [None, ARN])
def test_legacy_sam_deploy_requires_explicit_runtime_without_reading_deleted_config(
    tmp_path, explicit_arn
):
    import os
    import subprocess

    binary = tmp_path / "aws"
    marker = tmp_path / "aws-called"
    binary.write_text('#!/bin/sh\ntouch "$AWS_CALLED"\nexit 42\n')
    binary.chmod(0o755)
    env = {
        **os.environ,
        "PATH": str(tmp_path) + ":" + os.environ["PATH"],
        "AWS_CALLED": str(marker),
        "AWS_REGION": "us-east-1",
    }
    env.pop("AGENT_RUNTIME_ARN", None)
    if explicit_arn:
        env["AGENT_RUNTIME_ARN"] = explicit_arn
    result = subprocess.run(
        ["bash", "scripts/deploy.sh"], env=env, capture_output=True, text=True, timeout=10
    )
    if explicit_arn:
        assert result.returncode == 42 and marker.exists()
    else:
        assert result.returncode == 1 and not marker.exists()
        assert "Set AGENT_RUNTIME_ARN explicitly" in result.stderr
        assert "docs/fieldecho-deployment.md" in result.stderr


def test_release_timeout_covers_agent_and_console_poll_windows():
    import math

    import yaml

    workflow = yaml.safe_load(Path(".github/workflows/main.yml").read_text())
    # Agent READY, DEFAULT propagation, console SSM polling, then ten-minute command buffer.
    minimum = math.ceil((600 + 300 + 1950) / 60) + 10
    assert workflow["jobs"]["release"]["timeout-minutes"] >= minimum
