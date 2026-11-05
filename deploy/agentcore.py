"""Build and release the tested AgentCore CodeZip; operator provisioning stays outside CI."""

from __future__ import annotations

import argparse
import base64
import dataclasses
import hashlib
import json
import sys
import tempfile
import time
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

try:
    from deploy.fieldecho import SHA, aws, command, main_head, require
except ModuleNotFoundError:
    from fieldecho import SHA, aws, command, main_head, require


@dataclass(frozen=True)
class Target:
    account: str
    region: str
    runtime_arn: str
    execution_role_arn: str
    package_bucket: str
    package_prefix: str
    github_repository: str

    @property
    def runtime_id(self):
        return self.runtime_arn.rsplit("/", 1)[1]

    @classmethod
    def parse(cls, data):
        if not isinstance(data, dict) or set(data) != {f.name for f in dataclasses.fields(cls)}:
            raise ValueError("invalid AgentCore target fields")
        t = cls(**data)
        for pattern, value, name in (
            ("047640882913", t.account, "account"),
            ("us-east-1", t.region, "region"),
            (
                r"arn:aws:bedrock-agentcore:us-east-1:047640882913:runtime/[A-Za-z][A-Za-z0-9_]{0,47}-[A-Za-z0-9]{10}",
                t.runtime_arn,
                "runtime",
            ),
            (
                r"arn:aws:iam::047640882913:role/[A-Za-z0-9+=,.@_/-]+",
                t.execution_role_arn,
                "execution role",
            ),
            (r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", t.package_bucket, "bucket"),
            (r"[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*", t.package_prefix, "package prefix"),
            (r"AWS-DG37/AWS-breaking-barriers-DG37", t.github_repository, "repository"),
        ):
            require(pattern, value, name)
        return t

    @classmethod
    def load(cls, path):
        return cls.parse(json.loads(Path(path).read_text()))


def validate_package(path, sha):
    require(SHA, sha, "full SHA")
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if archive.testzip() is not None or "main.py" not in names:
            raise ValueError("invalid package")
        for entry in archive.infolist():
            if entry.create_system != 3 or (entry.external_attr >> 16) & 0o777 != 0o644:
                raise ValueError("package permissions must be readable 0644 files")
        for name in names:
            parts = PurePosixPath(name).parts
            if (
                PurePosixPath(name).is_absolute()
                or ".." in parts
                or (parts and parts[0] in {"data", "cache", ".env"})
                or any(p in {"__pycache__", ".git"} for p in parts)
                or name.endswith((".pyc", ".jsonl"))
            ):
                raise ValueError("package contains local state or unsafe paths")
        if json.loads(archive.read("release.json")) != {"sha": sha}:
            raise ValueError("package revision differs from tested SHA")
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def zip_info(name):
    entry = zipfile.ZipInfo(name, (2020, 1, 1, 0, 0, 0))
    entry.create_system = 3
    entry.external_attr = 0o100644 << 16
    return entry


def build(source, dependencies, output, sha):
    require(SHA, sha, "full SHA")
    # Explicit runtime inputs exclude workshop datasets and all writable state.
    files = {p.name: p for p in source.glob("*.py")}
    files.update(
        {
            str(p.relative_to(source)): p
            for p in (source / "skills").rglob("*")
            if p.is_file() and p.suffix in {".py", ".md", ".json"}
        }
    )
    for p in dependencies.rglob("*"):
        if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc":
            name = str(p.relative_to(dependencies))
            if name in files:
                raise ValueError("dependency shadows application file")
            files[name] = p
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, path in sorted(files.items()):
            archive.writestr(
                zip_info(name),
                path.read_bytes(),
                compress_type=zipfile.ZIP_DEFLATED,
            )
        archive.writestr(
            zip_info("release.json"),
            json.dumps({"sha": sha}, sort_keys=True),
        )
    validate_package(output, sha)


def smoke(target, version):
    for _ in range(60):
        endpoint = aws(
            target,
            "bedrock-agentcore-control",
            "get-agent-runtime-endpoint",
            agent_runtime_id=target.runtime_id,
            endpoint_name="DEFAULT",
        )
        if endpoint["status"] == "READY" and endpoint["liveVersion"] == version:
            break
        if endpoint["status"] not in ("READY", "CREATING", "UPDATING"):
            raise RuntimeError("AgentCore DEFAULT endpoint failed")
        time.sleep(5)
    else:
        raise RuntimeError("AgentCore DEFAULT endpoint version timeout")
    with tempfile.TemporaryDirectory() as directory:
        output = Path(directory) / "options.json"
        command(
            [
                "aws",
                "--region",
                target.region,
                "bedrock-agentcore",
                "invoke-agent-runtime",
                "--agent-runtime-arn",
                target.runtime_arn,
                "--qualifier",
                "DEFAULT",
                "--runtime-session-id",
                str(uuid.uuid4()),
                "--content-type",
                "application/json",
                "--cli-binary-format",
                "raw-in-base64-out",
                "--payload",
                '{"action":"options"}',
                str(output),
            ]
        )
        value = json.loads(output.read_text())
        if value.get("ok") is not True or not isinstance(value.get("result"), dict):
            raise RuntimeError("AgentCore options smoke failed")


def deploy(target, package, sha):
    digest = validate_package(package, sha)
    if main_head(target) != sha:
        return {"outcome": "SUPERSEDED", "sha": sha}
    if aws(target, "sts", "get-caller-identity")["Account"] != target.account:
        raise ValueError("AWS caller account differs from target")
    current = aws(
        target, "bedrock-agentcore-control", "get-agent-runtime", agent_runtime_id=target.runtime_id
    )
    if (
        current["agentRuntimeArn"] != target.runtime_arn
        or current["roleArn"] != target.execution_role_arn
        or current["status"] not in ("READY", "UPDATE_FAILED")
    ):
        raise ValueError("runtime target identity, role or readiness mismatch")
    previous = current["agentRuntimeVersion"]
    print(json.dumps({"sha": sha, "previous_version": previous}), flush=True)
    key = f"{target.package_prefix}/{sha}/{digest}.zip"
    old_code = current["agentRuntimeArtifact"].get("codeConfiguration", {})
    location = old_code.get("code", {}).get("s3", {})
    if (
        current["status"] == "READY"
        and location.get("bucket") == target.package_bucket
        and location.get("prefix") == key
        and location.get("versionId") not in (None, "", "null")
    ):
        smoke(target, previous)
        return {"outcome": "ALREADY_DEPLOYED", "sha": sha, "version": previous}
    if (
        aws(
            target,
            "s3api",
            "get-bucket-versioning",
            bucket=target.package_bucket,
            expected_bucket_owner=target.account,
        ).get("Status")
        != "Enabled"
    ):
        raise ValueError("package bucket must have versioning enabled")
    try:
        uploaded = aws(
            target,
            "s3api",
            "head-object",
            bucket=target.package_bucket,
            key=key,
            expected_bucket_owner=target.account,
            checksum_mode="ENABLED",
        )
    except RuntimeError:
        # A failed HEAD grants no overwrite: the conditional PUT still refuses existing objects.
        uploaded = aws(
            target,
            "s3api",
            "put-object",
            bucket=target.package_bucket,
            key=key,
            body=str(Path(package).resolve()),
            if_none_match="*",
            checksum_sha256=base64.b64encode(bytes.fromhex(digest)).decode(),
            expected_bucket_owner=target.account,
            metadata={"sha": sha, "sha256": digest},
        )
    else:
        if (
            uploaded.get("Metadata") != {"sha": sha, "sha256": digest}
            or uploaded.get("ChecksumSHA256") != base64.b64encode(bytes.fromhex(digest)).decode()
            or uploaded.get("ContentLength") != Path(package).stat().st_size
        ):
            raise ValueError("existing immutable package verification failed")
    version = uploaded.get("VersionId")
    if not isinstance(version, str) or version in ("", "null"):
        raise ValueError("immutable package requires an S3 version ID")
    artifact = {
        "codeConfiguration": {
            "code": {"s3": {"bucket": target.package_bucket, "prefix": key, "versionId": version}},
            "runtime": "PYTHON_3_12",
            "entryPoint": ["main.py"],
        }
    }
    params = {
        k: current[v]
        for k, v in (
            ("network_configuration", "networkConfiguration"),
            ("protocol_configuration", "protocolConfiguration"),
            ("environment_variables", "environmentVariables"),
            ("authorizer_configuration", "authorizerConfiguration"),
            ("request_header_configuration", "requestHeaderConfiguration"),
            ("lifecycle_configuration", "lifecycleConfiguration"),
            ("description", "description"),
            ("metadata_configuration", "metadataConfiguration"),
            ("filesystem_configurations", "filesystemConfigurations"),
            ("capacity_provider_configuration", "capacityProviderConfiguration"),
            ("platform_version", "platformVersion"),
            ("allowed_ports", "allowedPorts"),
        )
        if v in current and current[v] is not None
    }
    if main_head(target) != sha:
        return {"outcome": "SUPERSEDED", "sha": sha}
    updated = aws(
        target,
        "bedrock-agentcore-control",
        "update-agent-runtime",
        agent_runtime_id=target.runtime_id,
        role_arn=target.execution_role_arn,
        agent_runtime_artifact=artifact,
        **params,
    )
    candidate = updated["agentRuntimeVersion"]
    for _ in range(120):
        state = aws(
            target,
            "bedrock-agentcore-control",
            "get-agent-runtime",
            agent_runtime_id=target.runtime_id,
            agent_runtime_version=candidate,
        )
        if state["status"] == "READY":
            if state["agentRuntimeArtifact"] != artifact:
                raise RuntimeError("runtime artifact verification failed")
            smoke(target, candidate)
            return {
                "outcome": "DEPLOYED",
                "sha": sha,
                "version": candidate,
                "previous_version": previous,
                "s3_version": version,
            }
        if state["status"] not in ("CREATING", "UPDATING"):
            raise RuntimeError("AgentCore release failed: " + state["status"])
        time.sleep(5)
    raise RuntimeError("AgentCore READY timeout; previous version recorded above")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["build", "deploy"])
    parser.add_argument("--sha", required=True)
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--target", type=Path, default=Path("deploy/agentcore-target.json"))
    parser.add_argument("--dependencies", type=Path)
    args = parser.parse_args()
    if args.action == "build":
        build(Path("agents"), args.dependencies, args.package, args.sha)
    else:
        print(json.dumps(deploy(Target.load(args.target), args.package, args.sha), sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        print("AgentCore delivery failed: " + str(error), file=sys.stderr)
        sys.exit(1)
