"""Opt-in private EC2 setup. Python 3.9 standard library; operator AWS CLI required."""

from __future__ import annotations

import argparse
import base64
import dataclasses
import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path("/opt/dg37")
PROJECT = "dg37-explorer-cloud"
SHA = r"[0-9a-f]{64}"
REGION = r"[a-z]{2}-[a-z]+-[0-9]"
ORIGIN = r"https://[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+"


class SetupError(Exception):
    """An operator-safe failure without subprocess output or private inputs."""


def require(pattern, value):
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise SetupError("Setup input or resource identity is invalid")
    return value


@dataclass(frozen=True)
class SetupSpec:
    account: str
    region: str
    namespace: str
    vpc_id: str
    subnet_id: str
    image: str
    commit: str
    dataset_bucket: str
    dataset_key: str
    dataset_version: str
    dataset_sha256: str
    auth_domain: str
    auth_client_id: str
    auth_issuer: str
    console_url: str

    @property
    def stack_name(self):
        return self.namespace + "-foundation"

    @classmethod
    def parse(cls, data):
        if not isinstance(data, dict) or set(data) != {f.name for f in dataclasses.fields(cls)}:
            raise SetupError("Setup specification fields do not match the contract")
        s = cls(**data)
        for pattern, value in (
            (r"[0-9]{12}", s.account),
            (REGION, s.region),
            (r"[a-z][a-z0-9-]{2,29}", s.namespace),
            (r"vpc-[0-9a-f]{17}", s.vpc_id),
            (r"subnet-[0-9a-f]{17}", s.subnet_id),
            (r"[0-9a-f]{40}", s.commit),
            (r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", s.dataset_bucket),
            (r"[A-Za-z0-9_./-]{1,512}", s.dataset_key),
            (r"[A-Za-z0-9_+=./-]{1,1024}", s.dataset_version),
            (SHA, s.dataset_sha256),
            (r"[a-z0-9]{1,128}", s.auth_client_id),
            (ORIGIN, s.console_url),
        ):
            require(pattern, value)
        require(
            re.escape(f"{s.account}.dkr.ecr.{s.region}.amazonaws.com/{s.namespace}-explorer")
            + "@sha256:"
            + SHA,
            s.image,
        )
        require(
            r"https://[a-z0-9][a-z0-9-]{1,61}[a-z0-9]\.auth\."
            + re.escape(s.region)
            + r"\.amazoncognito\.com",
            s.auth_domain,
        )
        require(
            re.escape(f"https://cognito-idp.{s.region}.amazonaws.com/{s.region}_")
            + r"[A-Za-z0-9]+",
            s.auth_issuer,
        )
        if (
            s.dataset_version == "null"
            or s.dataset_key.startswith("/")
            or any(part in ("", ".", "..") for part in s.dataset_key.split("/"))
            or ".." in s.dataset_bucket
            or re.fullmatch(r"[0-9.]+", s.dataset_bucket)
        ):
            raise SetupError("Dataset must be an explicit versioned object")
        return s


@dataclass(frozen=True)
class HostIdentity:
    stack_id: str
    instance_id: str
    volume_id: str
    vpc_origin_id: str
    security_group_id: str

    @classmethod
    def parse(cls, data, spec):
        if not isinstance(data, dict) or set(data) != {f.name for f in dataclasses.fields(cls)}:
            raise SetupError("Host identity fields do not match")
        h = cls(**data)
        for pattern, value in (
            (
                re.escape(
                    f"arn:aws:cloudformation:{spec.region}:{spec.account}:stack/{spec.stack_name}/"
                )
                + r"[0-9a-f-]{36}",
                h.stack_id,
            ),
            (r"i-[0-9a-f]{17}", h.instance_id),
            (r"vol-[0-9a-f]{17}", h.volume_id),
            (r"[A-Za-z0-9_-]{1,128}", h.vpc_origin_id),
            (r"sg-[0-9a-f]{17}", h.security_group_id),
        ):
            require(pattern, value)
        return h


def command(args, *, input=None, timeout=600):
    try:
        return subprocess.run(
            args,
            input=input,
            capture_output=True,
            text=True,
            check=True,
            timeout=timeout,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        raise SetupError(
            "Command failed; inspect the host or AWS privately before retrying"
        ) from None


def aws_cli(spec):
    def call(*args):
        result = command(
            ["aws", *args, "--region", spec.region, "--output", "json", "--no-cli-pager"]
        )
        return json.loads(result)

    return call


def stack_identity(spec, aws):
    if aws("sts", "get-caller-identity").get("Account") != spec.account:
        raise SetupError("AWS operator account does not match the specification")
    stacks = aws("cloudformation", "describe-stacks", "--stack-name", spec.stack_name)["Stacks"]
    if len(stacks) != 1:
        raise SetupError("Expected exactly one foundation stack")
    stack = stacks[0]
    if stack["StackStatus"] not in ("CREATE_COMPLETE", "UPDATE_COMPLETE"):
        raise SetupError("Foundation stack must be healthy and idle")
    tags = {t["Key"]: t["Value"] for t in stack.get("Tags", [])}
    output = {o["OutputKey"]: o["OutputValue"] for o in stack["Outputs"]}
    parameters = {p["ParameterKey"]: p["ParameterValue"] for p in stack["Parameters"]}
    expected_parameters = dict(
        Namespace=spec.namespace,
        VpcId=spec.vpc_id,
        SubnetId=spec.subnet_id,
        DatasetBucket=spec.dataset_bucket,
        DatasetKey=spec.dataset_key,
        DatasetVersion=spec.dataset_version,
    )
    if (
        tags.get("dg37:namespace") != spec.namespace
        or any(parameters.get(k) != v for k, v in expected_parameters.items())
        or output["VpcId"] != spec.vpc_id
        or output["SubnetId"] != spec.subnet_id
        or output["ConsoleUrl"] != spec.console_url
        or output["RepositoryUri"] != spec.image.split("@")[0]
    ):
        raise SetupError("Foundation outputs do not match this namespace and specification")
    identity = HostIdentity.parse(
        dict(
            stack_id=stack["StackId"],
            instance_id=output["InstanceId"],
            volume_id=output["VolumeId"],
            vpc_origin_id=output["VpcOriginId"],
            security_group_id=output["HostSecurityGroupId"],
        ),
        spec,
    )
    return identity, stack


def configure_origin(spec, aws):
    identity, stack = stack_identity(spec, aws)
    origin = aws("cloudfront", "get-vpc-origin", "--id", identity.vpc_origin_id)["VpcOrigin"]
    expected = f"arn:aws:ec2:{spec.region}:{spec.account}:instance/{identity.instance_id}"
    if origin["Status"] != "Deployed" or origin["VpcOriginEndpointConfig"]["Arn"] != expected:
        raise SetupError("Private VPC origin is not deployed for this instance")
    groups = aws(
        "ec2",
        "describe-security-groups",
        "--filters",
        json.dumps(
            [
                dict(Name="vpc-id", Values=[spec.vpc_id]),
                dict(Name="group-name", Values=["CloudFront-VPCOrigins-Service-SG*"]),
            ]
        ),
    )["SecurityGroups"]
    if len(groups) != 1:
        raise SetupError("CloudFront service group is missing or ambiguous")
    group = groups[0]
    group_id = require(r"sg-[0-9a-f]{17}", group["GroupId"])
    if (
        group["VpcId"] != spec.vpc_id
        or group["OwnerId"] != spec.account
        or not group["GroupName"].startswith("CloudFront-VPCOrigins-Service-SG")
    ):
        raise SetupError("CloudFront service group identity does not match")
    interfaces = aws(
        "ec2",
        "describe-network-interfaces",
        "--filters",
        json.dumps(
            [
                dict(Name="group-id", Values=[group_id]),
                dict(Name="vpc-id", Values=[spec.vpc_id]),
            ]
        ),
    )["NetworkInterfaces"]
    if not any(
        e.get("RequesterManaged") is True
        and e.get("VpcId") == spec.vpc_id
        and e.get("SubnetId") == spec.subnet_id
        and group_id in {g["GroupId"] for g in e.get("Groups", [])}
        for e in interfaces
    ):
        raise SetupError("No applicable service-managed origin interface in this subnet")
    parameters = stack["Parameters"]
    if {p["ParameterKey"]: p["ParameterValue"] for p in parameters}.get(
        "CloudFrontServiceSecurityGroupId"
    ) == group_id:
        return group_id
    if sum(p["ParameterKey"] == "CloudFrontServiceSecurityGroupId" for p in parameters) != 1:
        raise SetupError("Foundation ingress parameter is missing or duplicated")
    preserved = [
        dict(ParameterKey=p["ParameterKey"], ParameterValue=group_id)
        if p["ParameterKey"] == "CloudFrontServiceSecurityGroupId"
        else dict(ParameterKey=p["ParameterKey"], UsePreviousValue=True)
        for p in parameters
    ]
    aws(
        "cloudformation",
        "update-stack",
        "--stack-name",
        identity.stack_id,
        "--use-previous-template",
        "--capabilities",
        "CAPABILITY_IAM",
        "--parameters",
        json.dumps(preserved),
    )
    return group_id


def atomic_write(path, content, mode=0o600):
    fd, name = tempfile.mkstemp(prefix=".bootstrap-", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w") as target:
            target.write(content)
            target.flush()
            os.fsync(target.fileno())
        os.replace(name, path)
        directory = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def read_env(path):
    if (
        path.is_symlink()
        or not path.is_file()
        or path.stat().st_uid != os.geteuid()
        or stat.S_IMODE(path.stat().st_mode) != 0o600
    ):
        raise SetupError("Retained credentials must be private regular files")
    values = {}
    for line in path.read_text().splitlines():
        key, separator, value = line.partition("=")
        if not separator or key in values or not re.fullmatch(r"[A-Z_]+", key):
            raise SetupError("Retained environment file is invalid")
        values[key] = value
    return values


def explorer_env(spec, password):
    return dict(
        MEL_AWS_REGION=spec.region,
        MEL_DATABASE_HOST="127.0.0.1",
        MEL_DATABASE_PORT="55437",
        MEL_DATABASE_NAME="farmerchat",
        MEL_DATABASE_USER="mel",
        MEL_DATABASE_PASSWORD=password,
        MEL_DATABASE_SSLMODE="disable",
        MEL_PACK_URI="/data/insights.jsonl",
        MEL_AUTH_DOMAIN=spec.auth_domain,
        MEL_AUTH_CLIENT_ID=spec.auth_client_id,
        MEL_AUTH_ISSUER=spec.auth_issuer,
        MEL_CONSOLE_URL=spec.console_url,
    )


def compose(spec, root):
    return dict(
        services=dict(
            postgres=dict(
                image="postgres:16",
                env_file=[str(root / "database.env")],
                ports=["127.0.0.1:55437:5432"],
                restart="unless-stopped",
                volumes=[
                    dict(
                        type="bind",
                        source=str(root / "postgres"),
                        target="/var/lib/postgresql/data",
                    )
                ],
                healthcheck=dict(
                    test=["CMD-SHELL", "pg_isready -U mel -d farmerchat"],
                    interval="5s",
                    timeout="3s",
                    retries=30,
                ),
            ),
            explorer=dict(
                image=spec.image,
                env_file=[str(root / "explorer.env")],
                network_mode="host",
                entrypoint=["mel-explorer", "--host", "127.0.0.1", "--port", "8877", "--no-live"],
                restart="unless-stopped",
                healthcheck=dict(disable=True),
                volumes=[
                    dict(type="bind", source=str(root / "data"), target="/data", read_only=True)
                ],
                depends_on=dict(postgres=dict(condition="service_healthy")),
            ),
        )
    )


def bootstrap_host(spec, identity, root, runtime):
    if root.is_symlink():
        raise SetupError("State root must not be a symlink")
    runtime.mount(identity.volume_id, root)
    root.mkdir(mode=0o755, parents=True, exist_ok=True)
    state_path = root / "bootstrap-state.json"
    immutable = dict(spec=dataclasses.asdict(spec), host=dataclasses.asdict(identity))
    for path in root.iterdir():
        if path.is_symlink():
            raise SetupError("Retained state must not contain symlinks")
    if state_path.exists():
        state = json.loads(state_path.read_text())
        if state.get("identity") != immutable or state.get("phase") not in ("preparing", "ready"):
            raise SetupError(
                "Retained bootstrap identity does not match; recovery requires an operator"
            )
    else:
        if any(
            p.name != "lost+found" or not p.is_dir() or any(p.iterdir()) for p in root.iterdir()
        ):
            raise SetupError("State root has foreign contents without a bootstrap identity")
        state = dict(identity=immutable, phase="preparing")
        atomic_write(state_path, json.dumps(state, sort_keys=True))
    database_path, explorer_path = root / "database.env", root / "explorer.env"
    has_data = (root / "postgres").exists() and any((root / "postgres").iterdir())
    if not database_path.exists():
        if has_data or explorer_path.exists() or state["phase"] == "ready":
            raise SetupError(
                "Existing state lacks database credentials; restore the retained files"
            )
        password = secrets.token_hex(32)
        atomic_write(
            database_path,
            "POSTGRES_DB=farmerchat\nPOSTGRES_USER=mel\n" + "POSTGRES_PASSWORD=" + password + "\n",
        )
    database = read_env(database_path)
    if (
        set(database) != {"POSTGRES_DB", "POSTGRES_USER", "POSTGRES_PASSWORD"}
        or database["POSTGRES_DB"] != "farmerchat"
        or database["POSTGRES_USER"] != "mel"
    ):
        raise SetupError("Retained database credentials do not match bootstrap")
    password = require(r"[0-9a-f]{64}", database["POSTGRES_PASSWORD"])
    expected_env = explorer_env(spec, password)
    adopted = (root / "delivery-state.json").exists()
    if explorer_path.exists():
        current_env = read_env(explorer_path)
        if current_env != expected_env:
            raise SetupError("Retained explorer environment differs; review it without overwriting")
    elif has_data or state["phase"] == "ready":
        raise SetupError("Existing database lacks explorer credentials; restore the retained files")
    else:
        atomic_write(explorer_path, "".join(k + "=" + v + "\n" for k, v in expected_env.items()))
    if adopted:
        runtime.verify(spec, root, adopted=True)
        return "delivery-owned"
    if state["phase"] == "ready":
        if (root / "COMMIT").read_text() != spec.commit + "\n":
            raise SetupError("Bootstrap commit changed without a delivery record")
        if json.loads((root / "compose.yaml").read_text()) != compose(spec, root):
            raise SetupError("Bootstrap Compose changed without a delivery record")
        runtime.verify(spec, root)
        return "ready"
    for name in ("postgres", "data", "backups"):
        (root / name).mkdir(mode=0o755, exist_ok=True)
    compose_path = root / "compose.yaml"
    rendered = json.dumps(compose(spec, root), sort_keys=True, indent=2) + "\n"
    if compose_path.exists() and compose_path.read_text() != rendered:
        raise SetupError("Interrupted bootstrap Compose differs; restore it before retrying")
    atomic_write(compose_path, rendered)
    runtime.prepare(spec, root)
    runtime.migrate(spec, root)
    runtime.verify(spec, root)
    runtime.proxy(spec, root)
    atomic_write(root / "COMMIT", spec.commit + "\n")
    state["phase"] = "ready"
    atomic_write(state_path, json.dumps(state, sort_keys=True))
    return "ready"


MIGRATE = """
from mel.config import Settings
from mel.store import Store
s=Store(Settings())
try:
    s.initialize()
    assert s.connection.execute('SELECT budget_usd FROM spend_cap').fetchone()[0] is None
finally: s.close()
"""
PROBE = """
from pathlib import Path
from mel.config import Settings
from mel.store import Store
s=Settings()
assert all((s.auth_domain,s.auth_client_id,s.auth_issuer,s.console_url))
assert not s.job_cluster and not s.job_task_definition
assert b'--no-live' in Path('/proc/1/cmdline').read_bytes().split(b'\\0')
assert b'--allow-live' not in Path('/proc/1/cmdline').read_bytes().split(b'\\0')
assert Path(s.pack_uri).is_file() and Path(s.pack_uri).stat().st_size > 0
db=Store(s)
try:
    assert db.connection.execute('SELECT 1').fetchone()[0]==1
    assert db.connection.execute('SELECT count(*) FROM schema_migrations').fetchone()[0]>0
finally: db.close()
"""
HTTP_PROBE = """
import urllib.request, urllib.error
for path,expected in [('/api/health',401),('/',200),('/static/app.mjs',200)]:
    try: r=urllib.request.urlopen('http://127.0.0.1:8877'+path,timeout=5)
    except urllib.error.HTTPError as error: r=error
    assert r.status==expected
    r.close()
"""


class DockerHost:
    def mount(self, volume_id, root):
        devices = json.loads(
            command(
                ["lsblk", "--json", "--paths", "--output", "NAME,SERIAL,FSTYPE,MOUNTPOINTS,TYPE"]
            )
        )["blockdevices"]
        matches = [d for d in devices if d.get("serial") == volume_id.replace("-", "")]
        if len(matches) != 1:
            raise SetupError("Expected retained EBS device is not uniquely attached")
        device = matches[0]
        mounts = [m for m in device.get("mountpoints", []) if m]
        if device["type"] != "disk" or device.get("children") or mounts not in ([], [str(root)]):
            raise SetupError("EBS device is occupied or mounted elsewhere")
        root.mkdir(mode=0o755, parents=True, exist_ok=True)
        if not mounts and any(root.iterdir()):
            raise SetupError("Refusing to hide existing state beneath a new mount")
        if not device.get("fstype"):
            signatures = json.loads(command(["wipefs", "--no-act", "--json", device["name"]]))
            if mounts or signatures.get("signatures"):
                raise SetupError("Refusing to format occupied storage")
            command(["mkfs.ext4", "-F", device["name"]])
        elif device["fstype"] != "ext4":
            raise SetupError("Retained EBS filesystem is not ext4")
        if not mounts:
            command(["mount", device["name"], str(root)])

    def persist_mount(self, root):
        uuid = command(
            ["findmnt", "--noheadings", "--output", "UUID", "--target", str(root)]
        ).strip()
        require(r"[0-9a-f-]{36}", uuid)
        entry = f"UUID={uuid} {root} ext4 defaults 0 2"
        fstab = Path("/etc/fstab")
        lines = fstab.read_text().splitlines()
        existing = [
            line for line in lines if len(line.split()) > 1 and line.split()[1] == str(root)
        ]
        if existing and existing != [entry]:
            raise SetupError("Existing retained mount configuration differs")
        if not existing:
            atomic_write(fstab, "\n".join(lines + [entry]) + "\n", 0o644)
        override = Path("/etc/systemd/system/docker.service.d")
        override.mkdir(parents=True, exist_ok=True)
        atomic_write(override / "dg37.conf", "[Unit]\nRequiresMountsFor=/opt/dg37\n", 0o644)
        command(["systemctl", "daemon-reload"])

    def compose_command(self, root, *args):
        return command(
            ["docker", "compose", "-p", PROJECT, "-f", str(root / "compose.yaml"), *args]
        )

    def prepare(self, spec, root):
        self.persist_mount(root)
        if command(["docker", "compose", "version", "--short"]).strip() != "2.39.4":
            raise SetupError("Expected pinned Docker Compose version is not installed")
        registry = spec.image.split("/", 1)[0]
        password = command(["aws", "ecr", "get-login-password", "--region", spec.region])
        command(
            ["docker", "login", "--username", "AWS", "--password-stdin", registry], input=password
        )
        try:
            command(["docker", "pull", spec.image])
        finally:
            command(["docker", "logout", registry])
        image = json.loads(command(["docker", "image", "inspect", spec.image]))[0]
        if (
            image["Config"].get("Labels", {}).get("org.opencontainers.image.revision")
            != spec.commit
            or image.get("Architecture") != "amd64"
        ):
            raise SetupError("Image architecture or full revision does not match")
        data = root / "data" / "insights.jsonl"
        if data.exists():
            if file_sha256(data) != spec.dataset_sha256:
                raise SetupError("Retained dataset checksum differs")
        else:
            staged = root / "data" / ".insights.staging"
            command(
                [
                    "aws",
                    "s3api",
                    "get-object",
                    "--bucket",
                    spec.dataset_bucket,
                    "--key",
                    spec.dataset_key,
                    "--version-id",
                    spec.dataset_version,
                    "--region",
                    spec.region,
                    str(staged),
                ]
            )
            if not staged.stat().st_size or file_sha256(staged) != spec.dataset_sha256:
                staged.unlink()
                raise SetupError("Downloaded dataset checksum differs")
            staged.chmod(0o644)
            os.replace(staged, data)
        data.chmod(0o644)
        self.compose_command(root, "config", "--quiet")
        self.compose_command(root, "up", "-d", "--wait", "--wait-timeout", "180", "postgres")

    def migrate(self, spec, root):
        command(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "host",
                "--env-file",
                str(root / "explorer.env"),
                "--mount",
                f"type=bind,src={root}/data,dst=/data,readonly",
                "--entrypoint",
                "python",
                spec.image,
                "-c",
                MIGRATE,
            ]
        )
        self.compose_command(root, "up", "-d", "--no-build", "explorer")

    def verify(self, spec, root, adopted=False):
        current = json.loads(
            self.compose_command(root, "config", "--no-env-resolution", "--format", "json")
        )
        if set(current["services"]) != {"postgres", "explorer"}:
            raise SetupError("Only the explorer and database services are allowed")
        app = current["services"]["explorer"]
        if app.get("network_mode") != "host" or app.get("ports") or app.get("networks"):
            raise SetupError("Explorer must use host loopback networking")
        image = app["image"]
        if not adopted and image != spec.image:
            raise SetupError("Bootstrap image differs from its immutable specification")
        sha = (root / "COMMIT").read_text().strip() if adopted else spec.commit
        require(r"[0-9a-f]{40}", sha)
        require(re.escape(spec.image.split("@")[0]) + r"@sha256:" + SHA, image)
        expected = json.loads(command(["docker", "image", "inspect", image]))[0]
        if expected["Config"].get("Labels", {}).get("org.opencontainers.image.revision") != sha:
            raise SetupError("Retained release image and commit do not match")
        effective = json.loads(self.compose_command(root, "config", "--format", "json"))
        env = effective["services"]["explorer"].get("environment", {})
        if {k: v for k, v in env.items() if k.startswith("MEL_")} != read_env(
            root / "explorer.env"
        ):
            raise SetupError("Compose and retained explorer environment differ")
        if file_sha256(root / "data" / "insights.jsonl") != spec.dataset_sha256:
            raise SetupError("Retained dataset checksum differs")
        for container in command(["docker", "ps", "-aq"]).split():
            info = json.loads(command(["docker", "inspect", container]))[0]
            if "mel-worker" in " ".join([info.get("Path", "")] + info.get("Args", [])):
                raise SetupError("A worker container is present; stop it explicitly")
        container = PROJECT + "-explorer-1"
        for attempt in range(45):
            try:
                actual = json.loads(command(["docker", "inspect", container]))[0]
                if actual["Image"] != expected["Id"]:
                    raise SetupError("Serving image differs from retained Compose")
                actual_env = dict(value.split("=", 1) for value in actual["Config"]["Env"])
                if {k: v for k, v in actual_env.items() if k.startswith("MEL_")} != read_env(
                    root / "explorer.env"
                ):
                    raise SetupError("Serving application environment differs from retained files")
                command(["docker", "exec", container, "python", "-c", PROBE])
                command(["docker", "exec", container, "python", "-c", HTTP_PROBE])
                return
            except SetupError:
                if attempt == 44:
                    raise
                time.sleep(2)

    def proxy(self, spec, root):
        config = """events {}
http {
  server {
    listen 80 default_server;
    location / {
      proxy_pass http://127.0.0.1:8877;
      proxy_set_header Host $host;
      proxy_set_header X-Forwarded-Proto https;
      proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }
  }
}
"""
        retained = root / "nginx.conf"
        atomic_write(retained, config, 0o644)
        command(["nginx", "-t", "-c", str(retained)])
        atomic_write(Path("/etc/nginx/nginx.conf"), config, 0o644)
        command(["systemctl", "reload", "nginx"])


def file_sha256(path):
    digest = hashlib.sha256()
    try:
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size == 0:
                raise SetupError("Dataset must be a nonempty regular file")
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
    except OSError:
        raise SetupError("Dataset is missing or unsafe to read") from None
    return digest.hexdigest()


def ssm_parameters(spec, identity, action):
    source = base64.b64encode(Path(__file__).read_bytes()).decode("ascii")
    payload = base64.b64encode(
        json.dumps(
            dict(
                spec=dataclasses.asdict(spec),
                identity=dataclasses.asdict(identity),
            ),
            sort_keys=True,
        ).encode()
    ).decode("ascii")
    if action not in ("bootstrap", "status"):
        raise SetupError("Invalid host operation")
    script = "\n".join(
        [
            "python3 - <<'DG37_SETUP'",
            "import base64, os, subprocess, tempfile",
            "source = base64.b64decode('" + source + "')",
            "payload = base64.b64decode('" + payload + "')",
            "fd, path = tempfile.mkstemp(prefix='dg37-setup-', suffix='.py')",
            "try:",
            "    os.fchmod(fd, 0o600)",
            "    with os.fdopen(fd, 'wb') as f: f.write(source)",
            "    result = subprocess.run(['python3', path, '_host-" + action + "'], input=payload)",
            "finally:",
            "    os.unlink(path)",
            "raise SystemExit(result.returncode)",
            "DG37_SETUP",
        ]
    )
    return dict(commands=[script], executionTimeout=["1800"])


def validate_auth(spec, aws):
    pool_id = spec.auth_issuer.rsplit("/", 1)[1]
    pool = aws("cognito-idp", "describe-user-pool", "--user-pool-id", pool_id)["UserPool"]
    app = aws(
        "cognito-idp",
        "describe-user-pool-client",
        "--user-pool-id",
        pool_id,
        "--client-id",
        spec.auth_client_id,
    )["UserPoolClient"]
    if (
        pool.get("Arn") != f"arn:aws:cognito-idp:{spec.region}:{spec.account}:userpool/{pool_id}"
        or pool.get("AdminCreateUserConfig", {}).get("AllowAdminCreateUserOnly") is not True
        or spec.auth_domain != f"https://{pool.get('Domain')}.auth.{spec.region}.amazoncognito.com"
        or app.get("UserPoolId") != pool_id
        or app.get("ClientId") != spec.auth_client_id
        or app.get("ClientSecret")
        or app.get("AllowedOAuthFlowsUserPoolClient") is not True
        or app.get("AllowedOAuthFlows") != ["code"]
        or set(app.get("AllowedOAuthScopes", [])) != {"openid", "email"}
        or app.get("SupportedIdentityProviders") != ["COGNITO"]
        or app.get("CallbackURLs") != [spec.console_url + "/api/auth/callback"]
        or app.get("LogoutURLs") != [spec.console_url + "/"]
    ):
        raise SetupError("Cognito must be the closed public code-flow client for this console")


def host_operation(spec, aws, action):
    identity, stack = stack_identity(spec, aws)
    if not {p["ParameterKey"]: p["ParameterValue"] for p in stack["Parameters"]}.get(
        "CloudFrontServiceSecurityGroupId"
    ):
        raise SetupError("Configure the private origin ingress before bootstrap")
    volumes = aws("ec2", "describe-volumes", "--volume-ids", identity.volume_id)["Volumes"]
    if (
        len(volumes) != 1
        or volumes[0].get("VolumeId") != identity.volume_id
        or volumes[0].get("Encrypted") is not True
        or len(volumes[0].get("Attachments", [])) != 1
        or not any(
            a.get("InstanceId") == identity.instance_id and a.get("State") == "attached"
            for a in volumes[0].get("Attachments", [])
        )
    ):
        raise SetupError("Encrypted retained volume is not attached to the expected host")
    validate_auth(spec, aws)
    response = aws(
        "ssm",
        "send-command",
        "--document-name",
        "AWS-RunShellScript",
        "--instance-ids",
        identity.instance_id,
        "--parameters",
        json.dumps(ssm_parameters(spec, identity, action)),
        "--timeout-seconds",
        "60",
    )
    command_id = require(r"[0-9a-f-]{36}", response["Command"]["CommandId"])
    print(json.dumps(dict(command_id=command_id, outcome="submitted")), flush=True)
    deadline = time.monotonic() + 1910
    while time.monotonic() < deadline:
        invocations = aws(
            "ssm",
            "list-command-invocations",
            "--command-id",
            command_id,
            "--instance-id",
            identity.instance_id,
        )["CommandInvocations"]
        if invocations:
            status = invocations[0]["Status"]
            if status == "Success":
                return dict(command_id=command_id, outcome="verified", action=action)
            if status not in ("Pending", "InProgress", "Delayed"):
                raise SetupError(
                    "Host operation failed; inspect SSM privately using its command ID"
                )
        time.sleep(5)
    raise SetupError("Host operation timed out; inspect SSM before submitting another operation")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=["configure-origin", "bootstrap", "status", "_host-bootstrap", "_host-status"],
    )
    parser.add_argument("--spec", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.action.startswith("_host-"):
            if os.geteuid() != 0:
                raise SetupError("Host setup requires the operator's root SSM context")
            payload = json.load(sys.stdin)
            if set(payload) != {"spec", "identity"}:
                raise SetupError("Host payload fields do not match")
            spec = SetupSpec.parse(payload["spec"])
            identity = HostIdentity.parse(payload["identity"], spec)
            if args.action == "_host-bootstrap":
                command(["cloud-init", "status", "--wait"], timeout=600)
            with Path("/run/dg37-bootstrap.lock").open("w") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                if args.action == "_host-status":
                    state = json.loads((ROOT / "bootstrap-state.json").read_text())
                    if state.get("phase") != "ready" or state.get("identity") != dict(
                        spec=dataclasses.asdict(spec), host=dataclasses.asdict(identity)
                    ):
                        raise SetupError("Retained bootstrap identity differs")
                    DockerHost().verify(spec, ROOT, adopted=(ROOT / "delivery-state.json").exists())
                    result = dict(outcome="verified")
                else:
                    result = dict(outcome=bootstrap_host(spec, identity, ROOT, DockerHost()))
        else:
            if args.spec is None:
                raise SetupError("Supply the private setup specification")
            spec = SetupSpec.parse(json.loads(args.spec.read_text()))
            aws = aws_cli(spec)
            if args.action == "configure-origin":
                result = dict(service_group=configure_origin(spec, aws), outcome="update-submitted")
            else:
                result = host_operation(spec, aws, args.action)
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception:
        parser.exit(
            1,
            "Setup failed; inspect private inputs, AWS command status, and retained host state.\n",
        )


if __name__ == "__main__":
    raise SystemExit(main())
