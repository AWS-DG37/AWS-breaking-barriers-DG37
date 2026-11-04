"""PostgreSQL checkpoints and transactional scheduler outbox.

One worker owns a run (session advisory lock). Its bounded coroutines serialize
short DB operations through this connection; model calls remain concurrent.
"""

import json
from hashlib import sha256
from pathlib import Path
from threading import RLock
from typing import Literal
from uuid import uuid4

import boto3

from mel.bundle import definition_sha256
from mel.config import Settings
from mel.datasets import InsightFilter, index_columns
from mel.models import Outcome

RunPurpose = Literal["handoff", "experiment"]
JobKind = Literal["batch", "experiment"]
JobStatus = Literal["queued", "running", "completed", "completed_with_errors", "failed"]


class Store:
    def __init__(self, settings: Settings):
        import psycopg

        credentials = {
            "username": settings.database_user,
            "password": settings.database_password,
        }
        if settings.database_secret_arn:
            client = boto3.client("secretsmanager", region_name=settings.aws_region)
            secret = client.get_secret_value(SecretId=settings.database_secret_arn)
            credentials = json.loads(secret["SecretString"])
        self.connection = psycopg.connect(
            host=settings.database_host,
            port=settings.database_port,
            dbname=settings.database_name,
            user=credentials["username"],
            password=credentials["password"],
            sslmode=settings.database_sslmode,
            **(
                {"sslrootcert": settings.database_sslrootcert}
                if settings.database_sslrootcert
                else {}
            ),
            autocommit=True,
            connect_timeout=15,
        )
        self.lock = RLock()

    def initialize(self) -> None:
        with self.lock, self.connection.transaction():
            self.connection.execute("SELECT pg_advisory_xact_lock(31890241)")
            self.connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations "
                "(name text PRIMARY KEY, checksum text NOT NULL, "
                "applied_at timestamptz NOT NULL DEFAULT now())"
            )
            for migration in sorted(Path("migrations").glob("*.sql")):
                source = migration.read_bytes()
                checksum = sha256(source).hexdigest()
                previous = self.connection.execute(
                    "SELECT checksum FROM schema_migrations WHERE name=%s", (migration.name,)
                ).fetchone()
                if previous:
                    if previous[0] != checksum:
                        raise ValueError(f"Applied migration changed: {migration.name}")
                    continue
                self.connection.execute(source.decode())
                self.connection.execute(
                    "INSERT INTO schema_migrations(name,checksum) VALUES (%s,%s)",
                    (migration.name, checksum),
                )

    def start_run(
        self,
        run_id: str,
        fingerprint: str,
        manifest: dict,
        *,
        purpose: RunPurpose = "handoff",
        experiment_id: str | None = None,
        config_id: str | None = None,
        job_id: str | None = None,
    ) -> None:
        from psycopg.types.json import Jsonb

        if purpose not in ("handoff", "experiment") or (purpose == "experiment") != (
            experiment_id is not None
        ):
            raise ValueError("purpose and experiment binding must agree")
        with self.lock:
            locked = self.connection.execute(
                "SELECT pg_try_advisory_lock(hashtextextended(%s, 0))", (run_id,)
            ).fetchone()[0]
            if not locked:
                raise ValueError("another worker owns this run_id")
            with self.connection.transaction():
                self.connection.execute(
                    "INSERT INTO generation_runs"
                    "(run_id,fingerprint,manifest,purpose,experiment_id,config_id,job_id) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s) "
                    "ON CONFLICT (run_id) DO NOTHING",
                    (
                        run_id,
                        fingerprint,
                        Jsonb(manifest),
                        purpose,
                        experiment_id,
                        config_id,
                        job_id,
                    ),
                )
                previous = self.connection.execute(
                    "SELECT fingerprint,purpose,experiment_id FROM generation_runs WHERE run_id=%s",
                    (run_id,),
                ).fetchone()
                if previous[0] != fingerprint:
                    raise ValueError("run_id exists with different input/config; use a new ID")
                if (
                    previous[1] != purpose
                    or (str(previous[2]) if previous[2] else None) != experiment_id
                ):
                    raise ValueError("run_id exists with different purpose or experiment binding")
                self.connection.execute(
                    "UPDATE generation_runs SET status='running',finished_at=NULL WHERE run_id=%s",
                    (run_id,),
                )

    def completed(self, run_id: str, retry_failed: bool) -> set[str]:
        with self.lock:
            rows = self.connection.execute(
                "SELECT insight_id,status FROM insight_results WHERE run_id=%s", (run_id,)
            ).fetchall()
        return {key for key, status in rows if not retry_failed or status != "failed"}

    def next_survey_id(self) -> str:
        with self.lock:
            number = self.connection.execute("SELECT nextval('survey_number_seq')").fetchone()[0]
        return f"S-{number:010d}"

    def save(self, run_id: str, insight: dict, outcome: Outcome) -> None:
        from psycopg.types.json import Jsonb

        with self.lock, self.connection.transaction():
            run = self.connection.execute(
                "SELECT purpose FROM generation_runs WHERE run_id=%s FOR SHARE", (run_id,)
            ).fetchone()
            if run is None:
                raise ValueError("unknown run_id")
            self.connection.execute(
                "INSERT INTO insight_results(run_id,insight_id,farmer_id,status,insight,outcome) "
                "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT(run_id,insight_id) DO UPDATE "
                "SET status=EXCLUDED.status,outcome=EXCLUDED.outcome,updated_at=now()",
                (
                    run_id,
                    outcome.insight_id,
                    insight.get("farmer_id"),
                    outcome.status,
                    Jsonb(insight),
                    Jsonb(outcome.model_dump(mode="json", exclude_none=True)),
                ),
            )
            if outcome.status == "accepted" and run[0] == "handoff":
                survey = outcome.survey
                self.connection.execute(
                    "INSERT INTO survey_handoffs(survey_id,run_id,insight_id,farmer_id,"
                    "not_before,not_after,payload) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                    (
                        survey["survey_id"],
                        run_id,
                        outcome.insight_id,
                        survey["farmer_id"],
                        survey["send_window"]["not_before"],
                        survey["send_window"]["not_after"],
                        Jsonb(outcome.handoff),
                    ),
                )

    def results(self, run_id: str) -> list[dict]:
        with self.lock:
            return [
                row[0]
                for row in self.connection.execute(
                    "SELECT outcome FROM insight_results WHERE run_id=%s ORDER BY insight_id",
                    (run_id,),
                ).fetchall()
            ]

    def finish(self, run_id: str, status: str) -> None:
        with self.lock:
            self.connection.execute(
                "UPDATE generation_runs SET status=%s,finished_at=now() WHERE run_id=%s",
                (status, run_id),
            )

    def create_experiment(self, experiment_id: str, spec: dict, *, source: str = "live") -> None:
        from psycopg.types.json import Jsonb

        with self.lock:
            self.connection.execute(
                "INSERT INTO experiments(experiment_id,spec,source) VALUES (%s,%s,%s) "
                "ON CONFLICT (experiment_id) DO NOTHING",
                (experiment_id, Jsonb(spec), source),
            )
            previous = self.connection.execute(
                "SELECT spec,source FROM experiments WHERE experiment_id=%s", (experiment_id,)
            ).fetchone()
            if previous != (spec, source):
                raise ValueError("experiment_id exists with a different specification")

    def finish_experiment(self, experiment_id: str, status: str) -> None:
        with self.lock:
            self.connection.execute(
                "UPDATE experiments SET status=%s,finished_at=CASE WHEN %s='running' "
                "THEN NULL ELSE now() END WHERE experiment_id=%s",
                (status, status, experiment_id),
            )

    def list_experiments(self) -> list[dict]:
        with self.lock:
            rows = self.connection.execute(
                "SELECT experiment_id,spec,status,source,created_at FROM experiments "
                "ORDER BY created_at DESC LIMIT 200"
            ).fetchall()
        return [
            {
                "experiment_id": str(key),
                "name": spec["name"],
                "status": status,
                "source": source,
                "created_at": created.isoformat(),
                "candidate_count": len(spec["candidates"]),
                "insight_count": min(len(spec["insights"]), spec.get("row_limit", 3)),
            }
            for key, spec, status, source, created in rows
        ]

    def experiment_detail(self, experiment_id: str) -> dict | None:
        with self.lock:
            row = self.connection.execute(
                "SELECT spec,status,source,created_at FROM experiments WHERE experiment_id=%s",
                (experiment_id,),
            ).fetchone()
            if row is None:
                return None
            spec, status, source, created = row
            runs = []
            for run_id, manifest, run_status in self.connection.execute(
                "SELECT run_id,manifest,status FROM generation_runs WHERE experiment_id=%s "
                "ORDER BY created_at,run_id",
                (experiment_id,),
            ).fetchall():
                model = manifest.get("config", {}).get("generation", {})
                results = [
                    {"insight_id": key, "input": insight, "outcome": outcome}
                    for key, insight, outcome in self.connection.execute(
                        "SELECT insight_id,insight,outcome FROM insight_results "
                        "WHERE run_id=%s ORDER BY insight_id",
                        (run_id,),
                    ).fetchall()
                ]
                runs.append(
                    {
                        "run_id": str(run_id),
                        "label": manifest.get("label", "Recorded model"),
                        "model_id": model.get("model_id"),
                        "api": model.get("api", "responses"),
                        "status": run_status,
                        "manifest": manifest,
                        "results": results,
                    }
                )
            assessments = [
                r[0]
                for r in self.connection.execute(
                    "SELECT assessment FROM quality_assessments WHERE experiment_id=%s "
                    "ORDER BY assessment_id",
                    (experiment_id,),
                ).fetchall()
            ]
        return {
            "experiment_id": experiment_id,
            "name": spec["name"],
            "spec": spec,
            "status": status,
            "source": source,
            "created_at": created.isoformat(),
            "runs": runs,
            "assessments": assessments,
        }

    def save_assessment(self, experiment_id: str, assessment: dict) -> dict:
        from psycopg.types.json import Jsonb

        with self.lock:
            self.connection.execute(
                "INSERT INTO quality_assessments(experiment_id,assessment) VALUES (%s,%s)",
                (experiment_id, Jsonb(assessment)),
            )
        return assessment

    # Agent configuration versions. Versions are immutable; saving an identical
    # definition returns the existing version.

    def save_config(
        self, definition: dict, *, parent_id: str | None = None, note: str | None = None
    ) -> str:
        from psycopg.types.json import Jsonb

        digest = definition_sha256(definition)
        with self.lock:
            self.connection.execute(
                "INSERT INTO agent_configs(config_id,parent_id,definition,definition_sha256,note) "
                "VALUES (%s,%s,%s,%s,%s) ON CONFLICT (definition_sha256) DO NOTHING",
                (str(uuid4()), parent_id, Jsonb(definition), digest, note),
            )
            row = self.connection.execute(
                "SELECT config_id FROM agent_configs WHERE definition_sha256=%s", (digest,)
            ).fetchone()
        return str(row[0])

    def config(self, config_id: str) -> dict | None:
        with self.lock:
            row = self.connection.execute(
                "SELECT config_id,parent_id,status,note,created_at,definition_sha256,definition "
                "FROM agent_configs WHERE config_id=%s",
                (config_id,),
            ).fetchone()
        if row is None:
            return None
        return {**_config_summary(row[:6], row[6]), "definition": row[6]}

    def list_configs(self) -> list[dict]:
        with self.lock:
            rows = self.connection.execute(
                "SELECT config_id,parent_id,status,note,created_at,definition_sha256,"
                "definition->'config' FROM agent_configs ORDER BY created_at DESC LIMIT 500"
            ).fetchall()
        return [_config_summary(row[:6], {"config": row[6]}) for row in rows]

    def config_count(self) -> int:
        with self.lock:
            return self.connection.execute("SELECT count(*) FROM agent_configs").fetchone()[0]

    def active_config_id(self) -> str | None:
        with self.lock:
            row = self.connection.execute(
                "SELECT config_id FROM agent_configs WHERE status='active'"
            ).fetchone()
        return str(row[0]) if row else None

    def activate_config(self, config_id: str) -> None:
        with self.lock, self.connection.transaction():
            if not self.connection.execute(
                "SELECT 1 FROM agent_configs WHERE config_id=%s FOR UPDATE", (config_id,)
            ).fetchone():
                raise ValueError("unknown config_id")
            self.connection.execute(
                "UPDATE agent_configs SET status='retired' WHERE status='active' AND config_id<>%s",
                (config_id,),
            )
            self.connection.execute(
                "UPDATE agent_configs SET status='active' WHERE config_id=%s", (config_id,)
            )

    # Insight datasets. A dataset is identified by its source bytes; importing the
    # same file twice returns the existing dataset.

    def import_dataset(self, name: str, rows: list[dict], source_sha256: str) -> dict:
        from psycopg.types.json import Jsonb

        with self.lock, self.connection.transaction():
            created = self.connection.execute(
                "INSERT INTO datasets(dataset_id,name,source_sha256,row_count) "
                "VALUES (%s,%s,%s,%s) ON CONFLICT (source_sha256) DO NOTHING RETURNING dataset_id",
                (str(uuid4()), name, source_sha256, len(rows)),
            ).fetchone()
            if created:
                dataset_id = str(created[0])
                with self.connection.cursor() as cursor:
                    cursor.executemany(
                        "INSERT INTO insights(dataset_id,ordinal,insight_id,farmer_id,crop,"
                        "concern,resolution_type,quality_flag,created_at,body) "
                        "VALUES (%(dataset_id)s,%(ordinal)s,%(insight_id)s,%(farmer_id)s,"
                        "%(crop)s,%(concern)s,%(resolution_type)s,%(quality_flag)s,"
                        "%(created_at)s,%(body)s)",
                        [
                            {
                                **index_columns(row),
                                "dataset_id": dataset_id,
                                "ordinal": ordinal,
                                "body": Jsonb(row),
                            }
                            for ordinal, row in enumerate(rows, 1)
                        ],
                    )
            row = self.connection.execute(
                "SELECT dataset_id,name,row_count,created_at FROM datasets WHERE source_sha256=%s",
                (source_sha256,),
            ).fetchone()
        return {**_dataset_summary(row), "created": created is not None}

    def list_datasets(self) -> list[dict]:
        with self.lock:
            rows = self.connection.execute(
                "SELECT dataset_id,name,row_count,created_at FROM datasets ORDER BY created_at DESC"
            ).fetchall()
        return [_dataset_summary(row) for row in rows]

    def dataset(self, dataset_id: str) -> dict | None:
        with self.lock:
            row = self.connection.execute(
                "SELECT dataset_id,name,row_count,created_at FROM datasets WHERE dataset_id=%s",
                (dataset_id,),
            ).fetchone()
        return _dataset_summary(row) if row else None

    def insights(self, dataset_id: str, selection: InsightFilter, offset: int = 0) -> list[dict]:
        """Insight bodies in file order. Untyped NULL parameters are cast so an unset
        filter field matches every row."""
        with self.lock:
            rows = self.connection.execute(
                "SELECT body FROM insights WHERE dataset_id=%(dataset_id)s "
                "AND (%(farmer_id)s::text IS NULL OR farmer_id=%(farmer_id)s) "
                "AND (%(crop)s::text IS NULL OR crop=%(crop)s) "
                "AND (%(concern)s::text IS NULL OR concern=%(concern)s) "
                "AND (%(resolution_type)s::text IS NULL OR resolution_type=%(resolution_type)s) "
                "AND (%(quality_flag)s::text IS NULL OR quality_flag=%(quality_flag)s) "
                "AND (%(created_from)s::date IS NULL "
                "OR (created_at AT TIME ZONE 'Asia/Kolkata')::date >= %(created_from)s) "
                "AND (%(created_to)s::date IS NULL "
                "OR (created_at AT TIME ZONE 'Asia/Kolkata')::date <= %(created_to)s) "
                "ORDER BY ordinal LIMIT %(limit)s OFFSET %(offset)s",
                {**selection.model_dump(), "dataset_id": dataset_id, "offset": offset},
            ).fetchall()
        return [row[0] for row in rows]

    # Jobs. Workers claim queued jobs with SKIP LOCKED, so several runners can share
    # one queue without running a job twice.

    def create_job(self, job_id: str, kind: JobKind, spec: dict) -> dict:
        from psycopg.types.json import Jsonb

        with self.lock:
            self.connection.execute(
                "INSERT INTO jobs(job_id,kind,spec) VALUES (%s,%s,%s)",
                (job_id, kind, Jsonb(spec)),
            )
        return self.job(job_id)

    def claim_job(self, job_id: str | None = None, *, resume: bool = False) -> dict | None:
        """The next queued job, or one named job. `resume` also reclaims a named job
        that stopped running; its runs resume from their checkpoints."""
        claimable = ["queued"] + (["running", "failed", "completed_with_errors"] if resume else [])
        with self.lock, self.connection.transaction():
            row = self.connection.execute(
                "SELECT job_id FROM jobs WHERE status = ANY(%s) "
                "AND (%s::uuid IS NULL OR job_id=%s) "
                "ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED",
                (claimable, job_id, job_id),
            ).fetchone()
            if row is None:
                return None
            self.connection.execute(
                "UPDATE jobs SET status='running',started_at=now(),finished_at=NULL,error=NULL "
                "WHERE job_id=%s",
                (row[0],),
            )
        return self.job(str(row[0]))

    def job_progress(self, job_id: str, progress: dict) -> None:
        from psycopg.types.json import Jsonb

        with self.lock:
            self.connection.execute(
                "UPDATE jobs SET progress=%s WHERE job_id=%s", (Jsonb(progress), job_id)
            )

    def finish_job(self, job_id: str, status: JobStatus, error: str | None = None) -> None:
        with self.lock:
            self.connection.execute(
                "UPDATE jobs SET status=%s,error=%s,finished_at=now() WHERE job_id=%s",
                (status, error, job_id),
            )

    def job(self, job_id: str) -> dict | None:
        with self.lock:
            row = self.connection.execute(
                "SELECT job_id,kind,spec,status,progress,error,created_at,started_at,finished_at "
                "FROM jobs WHERE job_id=%s",
                (job_id,),
            ).fetchone()
            if row is None:
                return None
            runs = self.connection.execute(
                "SELECT r.run_id,r.status,r.config_id,"
                "coalesce(jsonb_object_agg(i.status,i.n) FILTER (WHERE i.status IS NOT NULL),"
                "'{}') FROM generation_runs r LEFT JOIN (SELECT run_id,status,count(*) n "
                "FROM insight_results GROUP BY run_id,status) i ON i.run_id=r.run_id "
                "WHERE r.job_id=%s GROUP BY r.run_id ORDER BY r.created_at",
                (job_id,),
            ).fetchall()
        return {
            **_job_summary(row),
            "runs": [
                {
                    "run_id": str(run_id),
                    "status": status,
                    "config_id": str(config_id) if config_id else None,
                    "counts": counts,
                }
                for run_id, status, config_id, counts in runs
            ],
        }

    def list_jobs(self, limit: int = 100) -> list[dict]:
        with self.lock:
            rows = self.connection.execute(
                "SELECT job_id,kind,spec,status,progress,error,created_at,started_at,finished_at "
                "FROM jobs ORDER BY created_at DESC LIMIT %s",
                (limit,),
            ).fetchall()
        return [_job_summary(row) for row in rows]

    def close(self) -> None:
        self.connection.close()


def _iso(value) -> str | None:
    return value.isoformat() if value else None


def _config_summary(row, definition: dict) -> dict:
    config_id, parent_id, status, note, created, digest = row
    config = definition["config"]
    return {
        "config_id": str(config_id),
        "parent_id": str(parent_id) if parent_id else None,
        "status": status,
        "note": note,
        "created_at": _iso(created),
        "definition_sha256": digest,
        "version": config.get("version"),
        "generation_model": config["generation"]["model_id"],
        "review_model": config["review"]["model_id"],
    }


def _dataset_summary(row) -> dict:
    dataset_id, name, row_count, created = row
    return {
        "dataset_id": str(dataset_id),
        "name": name,
        "row_count": row_count,
        "created_at": _iso(created),
    }


def _job_summary(row) -> dict:
    job_id, kind, spec, status, progress, error, created, started, finished = row
    return {
        "job_id": str(job_id),
        "kind": kind,
        "spec": spec,
        "status": status,
        "progress": progress,
        "error": error,
        "created_at": _iso(created),
        "started_at": _iso(started),
        "finished_at": _iso(finished),
    }
