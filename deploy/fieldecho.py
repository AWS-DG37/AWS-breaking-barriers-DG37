"""Main-only Field Echo delivery. Host paths require only Python 3.9's standard library."""

from __future__ import annotations

import argparse
import dataclasses
import fcntl
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

SHA = r"[0-9a-f]{40}"
DIGEST = r"sha256:[0-9a-f]{64}"
ROOT = Path("/opt/dg37")
PROJECT = "dg37-explorer-cloud"
AUTH_ENV = ("MEL_AUTH_DOMAIN", "MEL_AUTH_CLIENT_ID", "MEL_AUTH_ISSUER", "MEL_CONSOLE_URL")


def require(pattern, value, name):
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise ValueError("invalid " + name)
    return value


def positive_sequence(value):
    if type(value) is not int or value < 1 or value > 9223372036854775807:
        raise ValueError("sequence must be a positive integer")
    return value


@dataclass(frozen=True)
class Target:
    account: str
    region: str
    instance_id: str
    ecr_repository: str
    deploy_role_arn: str
    ssm_document: str
    ssm_document_version: str
    workflow: str
    github_repository: str

    @property
    def registry(self):
        return self.account + ".dkr.ecr." + self.region + ".amazonaws.com/" + self.ecr_repository

    @classmethod
    def parse(cls, data):
        if not isinstance(data, dict) or set(data) != {f.name for f in dataclasses.fields(cls)}:
            raise ValueError("target fields do not match the delivery contract")
        t = cls(**data)
        for pattern, value, name in (
            (r"[0-9]{12}", t.account, "account"),
            (r"[a-z]{2}-[a-z]+-[0-9]", t.region, "region"),
            (r"i-[0-9a-f]{17}", t.instance_id, "instance"),
            (r"[a-z0-9]+(?:[._/-][a-z0-9]+)*", t.ecr_repository, "repository"),
            (
                r"arn:aws:iam::" + t.account + r":role/[A-Za-z0-9+=,.@_-]+",
                t.deploy_role_arn,
                "role",
            ),
            (r"[A-Za-z0-9_-]+", t.ssm_document, "document"),
            (r"[1-9][0-9]*", t.ssm_document_version, "document version"),
            (r"\.github/workflows/[A-Za-z0-9_-]+\.ya?ml", t.workflow, "workflow"),
            (r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", t.github_repository, "GitHub repository"),
        ):
            require(pattern, value, name)
        return t

    @classmethod
    def load(cls, path):
        return cls.parse(json.loads(Path(path).read_text()))


@dataclass(frozen=True)
class Release:
    sha: str
    image: str
    sequence: int

    @classmethod
    def parse(cls, data, target):
        if not isinstance(data, dict) or set(data) != {"sha", "image", "sequence"}:
            raise ValueError("invalid release fields")
        require(SHA, data["sha"], "full SHA")
        require(re.escape(target.registry) + "@" + DIGEST, data["image"], "immutable image")
        positive_sequence(data["sequence"])
        return cls(**data)


@dataclass
class HostState:
    deployed: dict
    previous: dict | None
    highest_sequence: int
    workflow: str
    transition: dict | None = None
    last_outcome: str = "ADOPTED"


def command(args, *, cwd=None, input=None):
    """Do not expose subprocess output: Docker/env/DB failures may contain credentials."""
    result = subprocess.run(
        args, cwd=cwd, input=input, capture_output=True, text=True, timeout=1500
    )
    if result.returncode:
        raise RuntimeError("command failed: " + Path(args[0]).name)
    return result.stdout


def aws(target, service, operation, **parameters):
    args = ["aws", "--region", target.region, service, operation, "--output", "json"]
    for name, value in parameters.items():
        args += [
            "--" + name.replace("_", "-"),
            json.dumps(value) if isinstance(value, (dict, list)) else str(value),
        ]
    return json.loads(command(args))


def main_head(target):
    output = command(
        [
            "git",
            "ls-remote",
            "https://github.com/" + target.github_repository + ".git",
            "refs/heads/main",
        ]
    )
    return require(SHA, output.split()[0], "main head")


def dispatch(target, action, sha="0" * 40, digest="sha256:" + "0" * 64, sequence=1):
    response = aws(
        target,
        "ssm",
        "send-command",
        document_name=target.ssm_document,
        document_version=target.ssm_document_version,
        instance_ids=[target.instance_id],
        timeout_seconds=120,
        parameters={
            "Action": [action],
            "Sha": [sha],
            "Digest": [digest],
            "Sequence": [str(sequence)],
        },
    )
    command_id = response["Command"]["CommandId"]
    for _ in range(390):
        try:
            result = aws(
                target,
                "ssm",
                "get-command-invocation",
                command_id=command_id,
                instance_id=target.instance_id,
            )
        except RuntimeError:
            time.sleep(5)
            continue
        if result["Status"] in ("Pending", "InProgress", "Delayed"):
            time.sleep(5)
            continue
        lines = result.get("StandardOutputContent", "").splitlines()
        reports = [
            json.loads(line[len("DG37_RESULT ") :])
            for line in lines
            if line.startswith("DG37_RESULT ")
        ]
        if not reports or (
            result["Status"] != "Success"
            and reports[-1].get("outcome")
            not in ("FAILED_ROLLED_BACK", "FAILED_BEFORE_SWITCH", "REFUSED_STALE")
        ):
            raise RuntimeError("SSM delivery failed; inspect the constrained command invocation")
        return reports[-1]
    raise RuntimeError("SSM result timeout; inspect status before retrying")


def deploy(target, full_sha, workflow_sequence):
    require(SHA, full_sha, "full SHA")
    positive_sequence(workflow_sequence)
    if main_head(target) != full_sha:
        return {"outcome": "SUPERSEDED", "sha": full_sha}
    details = aws(
        target,
        "ecr",
        "describe-images",
        repository_name=target.ecr_repository,
        image_ids=[{"imageTag": full_sha}],
    )["imageDetails"]
    if len(details) != 1:
        raise ValueError("expected one immutable artifact")
    digest = require(DIGEST, details[0]["imageDigest"], "digest")
    # Check again immediately before dispatch; the host sequence fence handles queued commands.
    if main_head(target) != full_sha:
        return {"outcome": "SUPERSEDED", "sha": full_sha}
    return dispatch(target, "deploy", full_sha, digest, workflow_sequence)


def status(target):
    return dispatch(target, "status")


def atomic_write(path, content):
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".delivery-")
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def render_compose(current, image):
    if set(current["services"]) != {"postgres", "explorer"}:
        raise ValueError("unexpected service; worker must be absent")
    result = json.loads(json.dumps(current))
    app = result["services"]["explorer"]
    app.pop("build", None)
    app.pop("command", None)
    # Health is checked explicitly, including authenticated 401, outside Compose.
    app["healthcheck"] = {"disable": True}
    app["image"] = image
    app["network_mode"] = "host"
    app["entrypoint"] = ["mel-explorer", "--host", "127.0.0.1", "--port", "8877", "--no-live"]
    return result


def check_readiness(proof, health_status, health_body, static_statuses, worker_present):
    if proof.get("database") != "connected" or proof.get("live_enabled") is not False:
        raise ValueError("database or offline-mode proof failed")
    expected = 401 if proof.get("auth_required") else 200
    if health_status != expected:
        raise ValueError("unexpected authentication response")
    if health_status == 200 and health_body.get("database") != "connected":
        raise ValueError("health body reports database failure")
    if static_statuses != [200, 200] or worker_present:
        raise ValueError("static resources or stopped-worker proof failed")


def check_snapshot(before, after, packaged):
    if any(after["ledger"].get(name) != checksum for name, checksum in before["ledger"].items()):
        raise ValueError("existing migration ledger changed")
    if any(after["ledger"].get(name) != checksum for name, checksum in packaged.items()):
        raise ValueError("packaged migration missing or changed")
    if before["tables"] != after["tables"]:
        raise ValueError("existing database rows changed")


class Host:
    """The sole writer of the release record. Caller holds the host flock."""

    def __init__(self, root, target, runtime):
        self.root, self.target, self.runtime = root, target, runtime
        self.state_path = root / "delivery-state.json"
        self.checkpoint = lambda name: None

    def save(self, state):
        atomic_write(self.state_path, json.dumps(dataclasses.asdict(state), sort_keys=True))

    def read(self, adopt=True):
        if self.state_path.exists():
            raw = json.loads(self.state_path.read_text())
            state = HostState(**raw)
            if type(state.highest_sequence) is not int or state.highest_sequence < 0:
                raise ValueError("invalid persisted sequence")
            for release in (state.deployed, state.previous):
                if release is None:
                    continue
                if not isinstance(release, dict) or set(release) != {"sha", "image", "sequence"}:
                    raise ValueError("invalid persisted release")
                require(SHA, release["sha"], "persisted SHA")
                require(
                    "(?:" + re.escape(self.target.registry) + "@)?" + DIGEST,
                    release["image"],
                    "persisted image",
                )
                if (
                    type(release["sequence"]) is not int
                    or not 0 <= release["sequence"] <= state.highest_sequence
                ):
                    raise ValueError("invalid persisted release sequence")
            if state.transition:
                if set(state.transition) != {"healthy_compose", "candidate"}:
                    raise ValueError("invalid transition")
                Release.parse(state.transition["candidate"], self.target)
                healthy = json.loads(state.transition["healthy_compose"])
                if healthy["services"]["explorer"]["image"] != state.deployed["image"]:
                    raise ValueError("transition does not preserve deployed image")
            return state
        if not adopt:
            raise ValueError("no managed deployment state")
        return HostState(self.runtime.adopt(), None, 0, self.target.workflow)

    def recover(self, state):
        if state.transition:
            # Compose and state are NOT an atomic pair. Intent wins in either crash window.
            self.runtime.recover_cleanup(state.transition["candidate"])
            compose = state.transition["healthy_compose"]
            atomic_write(self.root / "compose.yaml", compose)
            self.runtime.switch(compose)
            self.runtime.smoke(state.deployed)
            atomic_write(self.root / "COMMIT", state.deployed["sha"] + "\n")
            state.transition = None
            state.last_outcome = "RECOVERED_PREVIOUS"
            self.save(state)
        return state

    def activate(self, release):
        state = self.recover(self.read())
        if state.workflow != self.target.workflow:
            raise ValueError("workflow identity changed; operator migration required")
        if release.sequence <= state.highest_sequence:
            return self.report(state, "REFUSED_STALE")
        state.highest_sequence = release.sequence
        self.save(state)  # Failed attempts consume their sequence too.
        candidate = dataclasses.asdict(release)
        switched = False
        try:
            self.runtime.preflight(candidate)
            healthy = self.runtime.compose(state.deployed)
            # The adopted image is also forced offline before any migration or reboot.
            atomic_write(self.root / "compose.yaml", healthy)
            state.transition = {"healthy_compose": healthy, "candidate": candidate}
            self.save(state)
            self.checkpoint("intent")
            self.runtime.rehearse_and_migrate(candidate, state.deployed)
            next_compose = self.runtime.compose(candidate)
            switched = True
            self.runtime.switch(next_compose)
            self.checkpoint("switched")
            self.runtime.smoke(candidate)
            atomic_write(self.root / "compose.yaml", next_compose)
            self.checkpoint("compose")
            atomic_write(self.root / "COMMIT", release.sha + "\n")
            committed = dataclasses.replace(
                state,
                previous=state.deployed,
                deployed=candidate,
                transition=None,
                last_outcome="DEPLOYED",
            )
            self.save(committed)
            return self.report(committed)
        except Exception:
            if state.transition:
                self.recover(state)
            state.last_outcome = "FAILED_ROLLED_BACK" if switched else "FAILED_BEFORE_SWITCH"
            self.save(state)
            return self.report(state)

    def report(self, state, outcome=None):
        return {
            "outcome": outcome or state.last_outcome,
            "deployed": state.deployed,
            "previous": state.previous,
            "highest_sequence": state.highest_sequence,
            "transition_pending": state.transition is not None,
            "serving_image": self.runtime.serving_image(),
        }


# Executed inside the application image; no credential values are printed.
DB_PROBE = """
import json, os
from mel.config import Settings
from mel.store import Store
s=Settings(); db=Store(s)
try:
    assert db.connection.execute("SELECT 1").fetchone()[0] == 1
    args=open('/proc/1/cmdline','rb').read().split(b'\\0')
    assert b'--no-live' in args and b'--allow-live' not in args
    names=('MEL_AUTH_DOMAIN','MEL_AUTH_CLIENT_ID','MEL_AUTH_ISSUER','MEL_CONSOLE_URL')
    fields=[bool(os.environ.get(k)) for k in names]
    assert all(fields) or not any(fields)
    print(json.dumps(dict(database='connected',live_enabled=False,auth_required=all(fields))))
finally: db.close()
"""
HTTP_PROBE = """
import json, urllib.request, urllib.error
results=[]
for path in ('/api/health','/','/static/app.mjs'):
    try: response=urllib.request.urlopen('http://127.0.0.1:8877'+path,timeout=10)
    except urllib.error.HTTPError as error: response=error
    body=response.read()
    results.append([response.status,json.loads(body) if path=='/api/health' else {}])
print(json.dumps(results))
"""
SNAPSHOT = """
import json, hashlib, os
from psycopg import sql
from mel.config import Settings
from mel.store import Store
s=Store(Settings())
try:
    ledger=dict(s.connection.execute('SELECT name,checksum FROM schema_migrations').fetchall())
    columns=json.loads(os.environ.get('MEL_DEPLOY_COLUMNS','null'))
    if columns is None:
        columns={}
        table_query=("SELECT tablename FROM pg_tables WHERE schemaname='public' "
                     "AND tablename <> 'schema_migrations'")
        column_query=("SELECT column_name FROM information_schema.columns "
                      "WHERE table_schema='public' AND table_name=%s ORDER BY ordinal_position")
        for name, in s.connection.execute(table_query):
            columns[name]=[r[0] for r in s.connection.execute(column_query,(name,))]
    tables={}
    for name, fields in columns.items():
        query=sql.SQL('SELECT row_to_json(t)::text FROM (SELECT {} FROM {}) t').format(
            sql.SQL(',').join(map(sql.Identifier,fields)),sql.Identifier(name))
        rows=sorted(r[0] for r in s.connection.execute(query))
        tables[name]=dict(rows=len(rows),hash=hashlib.sha256(json.dumps(rows).encode()).hexdigest())
    print(json.dumps(dict(ledger=ledger,tables=tables,columns=columns)))
finally: s.close()
"""
MIGRATE = """
from mel.config import Settings
from mel.store import Store
s=Store(Settings())
try: s.initialize(); print('migrated')
finally: s.close()
"""


class DockerHost:
    def __init__(self, root, target):
        self.root, self.target = root, target
        self.container = PROJECT + "-explorer-1"
        self.current = json.loads(
            self.compose_command("config", "--no-env-resolution", "--format", "json")
        )

    def compose_command(self, *args, file="compose.yaml", input=None):
        return command(
            ["docker", "compose", "-p", PROJECT, "-f", str(self.root / file), *args],
            cwd=self.root,
            input=input,
        )

    def inspect(self, reference):
        return json.loads(command(["docker", "inspect", reference]))[0]

    def adopt(self):
        image = self.inspect(self.container)["Image"]
        require(DIGEST, image, "running local image")
        sha = require(SHA, (self.root / "COMMIT").read_text().strip(), "adopted commit")
        # Retain a stable local tag as well as the immutable local ID. Never prune images here.
        command(["docker", "image", "tag", image, "dg37-explorer-cloud-explorer:rollback-" + sha])
        return {"sha": sha, "image": image, "sequence": 0}

    def serving_image(self):
        return self.inspect(self.container)["Image"]

    def worker_present(self):
        ids = command(["docker", "ps", "-aq"]).split()
        for container in ids:
            data = self.inspect(container)
            executable = " ".join([data.get("Path", "")] + data.get("Args", []))
            if "mel-worker" in executable:
                return True
        return False

    def preflight(self, release):
        self.sequence = release["sequence"]
        for name in ("explorer.env", "database.env"):
            if not (self.root / name).is_file():
                raise ValueError("required host environment file missing")
        if self.worker_present():
            raise ValueError("worker container present; operator must stop and remove it")
        if set(self.current["services"]) != {"postgres", "explorer"}:
            raise ValueError("compose must contain only postgres and explorer")
        image = self.inspect(release["image"])
        if (
            image["Config"].get("Labels", {}).get("org.opencontainers.image.revision")
            != release["sha"]
        ):
            raise ValueError("artifact revision label mismatch")
        self.run_image(
            release["image"],
            "from mel.config import Settings; from pathlib import Path; "
            "p=Path(Settings().pack_uri); assert p.is_file() and p.stat().st_size > 0",
        )
        self.run_image(
            release["image"],
            "from pathlib import Path; files=list(Path('migrations').glob('*.sql')); "
            "assert files; assert all(p.read_bytes() for p in files)",
        )

    def compose(self, release):
        return json.dumps(render_compose(self.current, release["image"]), sort_keys=True)

    def switch(self, compose):
        atomic_write(self.root / "compose.activation.json", compose)
        self.compose_command(
            "up",
            "-d",
            "--no-build",
            "--no-deps",
            "--force-recreate",
            "explorer",
            file="compose.activation.json",
        )

    def recover_cleanup(self, candidate):
        label = "fieldecho.delivery.sequence=" + str(candidate["sequence"])
        containers = command(["docker", "ps", "-aq", "--filter", "label=" + label]).split()
        if containers:
            command(["docker", "rm", "-f", *containers])
        values = dict(
            line.split("=", 1)
            for line in (self.root / "database.env").read_text().splitlines()
            if line and not line.startswith("#")
        )
        user = require(r"[A-Za-z_][A-Za-z0-9_]*", values["POSTGRES_USER"], "database user")
        self.pg(
            "dropdb", "--if-exists", "-U", user, "fieldecho_rehearsal_" + str(candidate["sequence"])
        )

    def run_image(self, image, script, database=None, columns=None):
        args = [
            "docker",
            "run",
            "--rm",
            "--label",
            "fieldecho.delivery.sequence=" + str(self.sequence),
            "--network",
            "host",
            "--volumes-from",
            self.container,
            "--env-file",
            str(self.root / "explorer.env"),
        ]
        if database:
            args += ["--env", "MEL_DATABASE_NAME=" + database]
        if columns is not None:
            args += ["--env", "MEL_DEPLOY_COLUMNS=" + json.dumps(columns)]
        args += ["--entrypoint", "python", image, "-c", script]
        return command(args, cwd=self.root)

    def smoke(self, release, container=None, port=8877):
        container = container or self.container
        expected_id = self.inspect(release["image"])["Id"]
        if self.inspect(container)["Image"] != expected_id:
            raise ValueError("serving container identity differs from release")
        for _ in range(45):
            try:
                proof = json.loads(command(["docker", "exec", container, "python", "-c", DB_PROBE]))
                responses = json.loads(
                    command(
                        [
                            "docker",
                            "exec",
                            container,
                            "python",
                            "-c",
                            HTTP_PROBE.replace("8877", str(port)),
                        ]
                    )
                )
                check_readiness(
                    proof,
                    responses[0][0],
                    responses[0][1],
                    [x[0] for x in responses[1:]],
                    self.worker_present(),
                )
                return
            except (RuntimeError, ValueError, KeyError):
                time.sleep(2)
        raise ValueError("serving readiness failed")

    def pg(self, *args, input=None):
        return self.compose_command("exec", "-T", "postgres", *args, input=input)

    def rehearse_and_migrate(self, release, previous):
        # ponytail: whole-DB rehearsal fits a workshop DB; minutes/GB is the ceiling.
        # For larger installations replace this with a reviewed migration-specific rehearsal.
        env = {}
        for line in (self.root / "database.env").read_text().splitlines():
            if line and not line.startswith("#"):
                key, value = line.split("=", 1)
                env[key] = value
        user, database = env["POSTGRES_USER"], env["POSTGRES_DB"]
        require(r"[A-Za-z_][A-Za-z0-9_]*", user, "database user")
        require(r"[A-Za-z_][A-Za-z0-9_]*", database, "database name")
        rehearsal = "fieldecho_rehearsal_" + str(release["sequence"])
        backup = self.root / "backups" / (str(release["sequence"]) + "-" + release["sha"])
        backup.mkdir(mode=0o700, parents=True, exist_ok=False)
        self.compose_command("stop", "explorer")  # Quiesce all application writers before snapshot.
        before = json.loads(self.run_image(release["image"], SNAPSHOT))
        atomic_write(backup / "before.json", json.dumps(before))
        # Plain SQL keeps binary dump bytes out of the text subprocess interface.
        dump = self.pg("pg_dump", "-U", user, "-d", database, "--no-owner", "--no-privileges")
        atomic_write(backup / "database.sql", dump)
        self.pg("createdb", "-U", user, rehearsal)
        try:
            self.pg("psql", "-X", "-v", "ON_ERROR_STOP=1", "-U", user, "-d", rehearsal, input=dump)
            packaged = json.loads(
                self.run_image(
                    release["image"],
                    "import json,hashlib; from pathlib import Path; "
                    "files=Path('migrations').glob('*.sql'); "
                    "print(json.dumps({p.name:hashlib.sha256(p.read_bytes()).hexdigest() "
                    "for p in files}))",
                )
            )
            for _ in range(2):
                self.run_image(release["image"], MIGRATE, rehearsal)
                after = json.loads(
                    self.run_image(release["image"], SNAPSHOT, rehearsal, before["columns"])
                )
                check_snapshot(before, after, packaged)
            # Prior release must read recorded experiments on the migrated DB, with live off.
            compatibility = """
from mel.config import Settings
from mel.store import Store
s=Store(Settings())
try:
    s.initialize()
    for row in s.list_experiments(): s.experiment_detail(str(row['experiment_id']))
    print('previous-compatible')
finally: s.close()
"""
            self.run_image(previous["image"], compatibility, rehearsal)
            old_container = "fieldecho-previous-rehearsal-" + str(release["sequence"])
            try:
                command(
                    [
                        "docker",
                        "run",
                        "-d",
                        "--rm",
                        "--name",
                        old_container,
                        "--label",
                        "fieldecho.delivery.sequence=" + str(release["sequence"]),
                        "--volumes-from",
                        self.container,
                        "--network",
                        "host",
                        "--env-file",
                        str(self.root / "explorer.env"),
                        "--env",
                        "MEL_DATABASE_NAME=" + rehearsal,
                        "--entrypoint",
                        "mel-explorer",
                        previous["image"],
                        "--host",
                        "127.0.0.1",
                        "--port",
                        "8878",
                        "--no-live",
                    ]
                )
                self.smoke(previous, old_container, 8878)
            finally:
                command(["docker", "rm", "-f", old_container])
            self.run_image(
                release["image"], MIGRATE
            )  # Explicit production migration before activation.
            production = json.loads(
                self.run_image(release["image"], SNAPSHOT, columns=before["columns"])
            )
            check_snapshot(before, production, packaged)
            atomic_write(
                backup / "migration-proof.json",
                json.dumps(
                    {"rehearsed_twice": True, "previous_compatible": True, "production": production}
                ),
            )
        finally:
            self.pg("dropdb", "--if-exists", "-U", user, rehearsal)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("deploy", "status", "host-activate", "host-status"))
    parser.add_argument("--target", type=Path, default=Path("deploy/target.json"))
    parser.add_argument("--sha")
    parser.add_argument("--digest")
    parser.add_argument("--sequence", type=int)
    args = parser.parse_args()
    try:
        target = Target.load(args.target)
        if args.action == "deploy":
            result = deploy(target, args.sha, args.sequence)
        elif args.action == "status":
            result = status(target)
        else:
            os.umask(0o077)
            with (ROOT / "delivery.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                runtime = DockerHost(ROOT, target)
                host = Host(ROOT, target, runtime)
                if args.action == "host-activate":
                    release = Release.parse(
                        {
                            "sha": args.sha,
                            "image": target.registry + "@" + str(args.digest),
                            "sequence": args.sequence,
                        },
                        target,
                    )
                    result = host.activate(release)
                else:
                    state = host.read(adopt=False)  # Status never changes host state.
                    result = host.report(state)
                    result["serving_image"] = runtime.inspect(runtime.container)["Image"]
                    result["worker_present"] = runtime.worker_present()
        print("DG37_RESULT " + json.dumps(result, sort_keys=True))
        return (
            0
            if result["outcome"] in ("DEPLOYED", "SUPERSEDED", "ADOPTED", "RECOVERED_PREVIOUS")
            or args.action.endswith("status")
            else 1
        )
    except Exception:
        print("delivery failed; no credentials or subprocess output are emitted", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
