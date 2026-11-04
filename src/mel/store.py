"""PostgreSQL checkpoints and transactional scheduler outbox.

One worker owns a run (session advisory lock). Its bounded coroutines serialize
short DB operations through this connection; model calls remain concurrent.
"""

import json
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from threading import RLock
from typing import Literal
from uuid import uuid4

import boto3

from mel.bundle import definition_sha256
from mel.clock import parse_ist
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

    def log_message(
        self,
        *,
        farmer_id: str,
        channel: str,
        message_type: str,
        status: str,
        survey_id: str | None = None,
        message_content: dict | None = None,
        external_message_id: str | None = None,
        sent_at: str | datetime | None = None,
        delivered_at: str | datetime | None = None,
        failed_at: str | datetime | None = None,
    ) -> dict:
        """Record one message event reported by an external sender.

        Event times (`sent_at`/`delivered_at`/`failed_at`) come from the sender and are
        optional; whichever are present are normalized to IST via parse_ist (naive values
        are rejected). `created_at` is set by the database to now(). The method never
        invents a send time and never reads it from the survey's send window; a message
        with no reported send time is logged with sent_at NULL (only allowed for failures).

        Idempotent on `external_message_id`: re-reporting the same platform id returns the
        existing row unchanged instead of inserting a duplicate. This holds across separate
        Store instances/processes: the insert uses ON CONFLICT DO NOTHING against the
        partial unique index, so a concurrent duplicate neither creates a second row nor
        raises a unique-violation; the losing caller re-reads and returns the existing row.
        A call with external_message_id=None has no conflict target and always inserts.
        """
        from psycopg.types.json import Jsonb

        new_id = str(uuid4())
        with self.lock, self.connection.transaction():
            inserted = self.connection.execute(
                "INSERT INTO message_logs(id,farmer_id,survey_id,channel,message_type,status,"
                "message_content,external_message_id,sent_at,delivered_at,failed_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (external_message_id) WHERE external_message_id IS NOT NULL "
                "DO NOTHING RETURNING id",
                (
                    new_id,
                    farmer_id,
                    survey_id,
                    channel,
                    message_type,
                    status,
                    Jsonb(message_content) if message_content is not None else None,
                    external_message_id,
                    _ist_or_none(sent_at),
                    _ist_or_none(delivered_at),
                    _ist_or_none(failed_at),
                ),
            ).fetchone()
            if inserted is None:
                # A row with this external_message_id already exists (possibly inserted
                # concurrently by another process). Return it unchanged.
                existing = self.connection.execute(
                    "SELECT id FROM message_logs WHERE external_message_id=%s",
                    (external_message_id,),
                ).fetchone()
                return self.message(str(existing[0]))
        return self.message(new_id)

    def message(self, message_id: str) -> dict | None:
        with self.lock:
            row = self.connection.execute(
                f"SELECT {_MESSAGE_COLUMNS} FROM message_logs WHERE id=%s", (message_id,)
            ).fetchone()
        return _message_log(row) if row else None

    def messages_for_farmer(self, farmer_id: str) -> list[dict]:
        with self.lock:
            rows = self.connection.execute(
                f"SELECT {_MESSAGE_COLUMNS} FROM message_logs WHERE farmer_id=%s "
                f"ORDER BY {_MESSAGE_EVENT_ORDER}",
                (farmer_id,),
            ).fetchall()
        return [_message_log(row) for row in rows]

    def messages_for_survey(self, survey_id: str) -> list[dict]:
        with self.lock:
            rows = self.connection.execute(
                f"SELECT {_MESSAGE_COLUMNS} FROM message_logs WHERE survey_id=%s "
                f"ORDER BY {_MESSAGE_EVENT_ORDER}",
                (survey_id,),
            ).fetchall()
        return [_message_log(row) for row in rows]

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
                "insight_count": len(spec["insights"]),
                "config_ids": [c["config_id"] for c in spec["candidates"] if c.get("config_id")],
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
            job = self.connection.execute(
                "SELECT status,progress,error,created_at FROM jobs WHERE kind='experiment' "
                "AND spec->>'experiment_id'=%s ORDER BY created_at DESC LIMIT 1",
                (experiment_id,),
            ).fetchone()
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
            "job": {
                "status": job[0],
                "progress": job[1],
                "error": job[2],
                "created_at": _iso(job[3]),
            }
            if job
            else None,
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
        self,
        definition: dict,
        *,
        parent_id: str | None = None,
        note: str | None = None,
        name: str | None = None,
    ) -> str:
        from psycopg.types.json import Jsonb

        digest = definition_sha256(definition)
        with self.lock:
            self.connection.execute(
                "INSERT INTO agent_configs"
                "(config_id,parent_id,definition,definition_sha256,note,name) "
                "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (definition_sha256) DO NOTHING",
                (str(uuid4()), parent_id, Jsonb(definition), digest, note, name),
            )
            row = self.connection.execute(
                "SELECT config_id FROM agent_configs WHERE definition_sha256=%s", (digest,)
            ).fetchone()
        return str(row[0])

    def config(self, config_id: str) -> dict | None:
        with self.lock:
            row = self.connection.execute(
                "SELECT config_id,parent_id,status,note,created_at,definition_sha256,name,"
                "definition FROM agent_configs WHERE config_id=%s",
                (config_id,),
            ).fetchone()
        if row is None:
            return None
        return {**_config_summary(row[:7], row[7]), "definition": row[7]}

    def list_configs(self) -> list[dict]:
        with self.lock:
            rows = self.connection.execute(
                "SELECT config_id,parent_id,status,note,created_at,definition_sha256,name,"
                "definition->'config' FROM agent_configs ORDER BY created_at DESC LIMIT 500"
            ).fetchall()
        return [_config_summary(row[:7], {"config": row[7]}) for row in rows]

    def active_config_id(self) -> str | None:
        with self.lock:
            row = self.connection.execute(
                "SELECT config_id FROM agent_configs WHERE status='active'"
            ).fetchone()
        return str(row[0]) if row else None

    def name_config(self, config_id: str, name: str, note: str | None = None) -> None:
        """Name a version and mark a draft saved: a runnable version people can pick."""
        with self.lock:
            updated = self.connection.execute(
                "UPDATE agent_configs SET name=%s,note=coalesce(%s,note),"
                "status=CASE WHEN status='draft' THEN 'saved' ELSE status END "
                "WHERE config_id=%s RETURNING config_id",
                (name, note, config_id),
            ).fetchone()
        if updated is None:
            raise ValueError("unknown config_id")

    def activate_config(self, config_id: str) -> None:
        with self.lock, self.connection.transaction():
            row = self.connection.execute(
                "SELECT status FROM agent_configs WHERE config_id=%s FOR UPDATE", (config_id,)
            ).fetchone()
            if row is None:
                raise ValueError("unknown config_id")
            if row[0] == "draft":
                raise ValueError("save this version before activating it")
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
        """Insight bodies in file order."""
        with self.lock:
            rows = self.connection.execute(
                f"SELECT body FROM insights WHERE {_SELECTION} "
                "ORDER BY ordinal LIMIT %(limit)s OFFSET %(offset)s",
                {**selection.model_dump(), "dataset_id": dataset_id, "offset": offset},
            ).fetchall()
        return [row[0] for row in rows]

    def count_insights(self, dataset_id: str, selection: InsightFilter) -> int:
        """Rows matching the selection, before its limit."""
        with self.lock:
            return self.connection.execute(
                f"SELECT count(*) FROM insights WHERE {_SELECTION}",
                {**selection.model_dump(), "dataset_id": dataset_id},
            ).fetchone()[0]

    def dataset_facets(self, dataset_id: str) -> dict:
        """Filter choices with row counts, and the India Standard Time date range."""
        facets = {}
        with self.lock:
            for column in ("crop", "concern", "resolution_type", "quality_flag"):
                rows = self.connection.execute(
                    f"SELECT {column}, count(*) FROM insights WHERE dataset_id=%s "
                    f"AND {column} IS NOT NULL GROUP BY {column} ORDER BY count(*) DESC, {column}",
                    (dataset_id,),
                ).fetchall()
                facets[column] = [{"value": value, "count": count} for value, count in rows]
            first, last = self.connection.execute(
                "SELECT min((created_at AT TIME ZONE 'Asia/Kolkata')::date), "
                "max((created_at AT TIME ZONE 'Asia/Kolkata')::date) FROM insights "
                "WHERE dataset_id=%s",
                (dataset_id,),
            ).fetchone()
        facets["created"] = {"from": _iso(first), "to": _iso(last)}
        return facets

    # Generation runs for the scheduler (purpose 'handoff'), as the console lists them.

    def list_runs(self, limit: int = 50) -> list[dict]:
        with self.lock:
            rows = self.connection.execute(
                f"{_RUN_SUMMARY} WHERE r.purpose='handoff' "
                "GROUP BY r.run_id,j.job_id ORDER BY r.created_at DESC LIMIT %s",
                (limit,),
            ).fetchall()
        return [_run_summary(row) for row in rows]

    def run_summary(self, run_id: str) -> dict | None:
        with self.lock:
            row = self.connection.execute(
                f"{_RUN_SUMMARY} WHERE r.run_id=%s GROUP BY r.run_id,j.job_id", (run_id,)
            ).fetchone()
        return _run_summary(row) if row else None

    def run_results(
        self, run_id: str, status: str | None = None, offset: int = 0, limit: int = 50
    ) -> list[dict]:
        """One page of results, newest checkpoint last, with the fields a results table
        shows. The full outcome comes from run_result."""
        with self.lock:
            rows = self.connection.execute(
                "SELECT insight_id,status,insight,outcome->>'rule',outcome->>'reason',"
                "outcome->>'detail',outcome->'survey'->'items'->0->>'prompt',"
                "outcome->'candidate_survey'->'items'->0->>'prompt',"
                "jsonb_array_length(coalesce(outcome->'survey'->'items','[]')),"
                "(outcome->'review'->>'relevance_score')::float,"
                "(outcome->>'elapsed_seconds')::float FROM insight_results "
                "WHERE run_id=%s AND (%s::text IS NULL OR status=%s) "
                "ORDER BY updated_at,insight_id LIMIT %s OFFSET %s",
                (run_id, status, status, limit, offset),
            ).fetchall()
        return [
            {
                "insight_id": key,
                "status": state,
                "insight": {
                    field: insight.get(field)
                    for field in (
                        "farmer_id",
                        "created_at",
                        "concern",
                        "conversation_language",
                        "query_summary",
                        "advice_summary",
                    )
                }
                | {"crop": (insight.get("asset") or {}).get("name")},
                "rule": rule,
                "reason": reason,
                "detail": detail,
                "first_question": question or draft_question,
                "questions": questions,
                "relevance": relevance,
                "elapsed_seconds": elapsed,
            }
            for (
                key,
                state,
                insight,
                rule,
                reason,
                detail,
                question,
                draft_question,
                questions,
                relevance,
                elapsed,
            ) in rows
        ]

    def run_result(self, run_id: str, insight_id: str) -> dict | None:
        with self.lock:
            row = self.connection.execute(
                "SELECT status,insight,outcome FROM insight_results "
                "WHERE run_id=%s AND insight_id=%s",
                (run_id, insight_id),
            ).fetchone()
        if row is None:
            return None
        return {"insight_id": insight_id, "status": row[0], "insight": row[1], "outcome": row[2]}

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


def _ist_or_none(value: str | datetime | None) -> datetime | None:
    """Normalize an externally reported event time to IST, or pass through None.

    Reuses parse_ist, so a naive timestamp is rejected rather than interpreted in the
    machine's timezone, exactly as the timing pipeline treats insight timestamps."""
    return None if value is None else parse_ist(value)


def _ist_iso(value: datetime | None) -> str | None:
    """Render a stored timestamptz in IST. PostgreSQL returns timestamptz in the
    session timezone (UTC here); the stored instant is correct, but message logs
    present every time in IST (+05:30), consistent with the rest of the pipeline."""
    return None if value is None else parse_ist(value).isoformat()


_MESSAGE_COLUMNS = (
    "id,farmer_id,survey_id,channel,message_type,status,message_content,"
    "external_message_id,sent_at,delivered_at,failed_at,created_at"
)

# Chronological event-time order: each event is ordered by the timestamp of its own
# status (a delivered event by delivered_at, not its original sent_at), falling back to
# created_at when that time is absent. created_at then id break ties deterministically.
_MESSAGE_EVENT_ORDER = (
    "coalesce("
    "CASE status WHEN 'sent' THEN sent_at "
    "WHEN 'delivered' THEN delivered_at "
    "WHEN 'failed' THEN failed_at END, created_at), created_at, id"
)


def _message_log(row) -> dict:
    (
        id_,
        farmer_id,
        survey_id,
        channel,
        message_type,
        status,
        message_content,
        external_message_id,
        sent_at,
        delivered_at,
        failed_at,
        created_at,
    ) = row
    return {
        "id": str(id_),
        "farmer_id": farmer_id,
        "survey_id": survey_id,
        "channel": channel,
        "message_type": message_type,
        "status": status,
        "message_content": message_content,
        "external_message_id": external_message_id,
        "sent_at": _ist_iso(sent_at),
        "delivered_at": _ist_iso(delivered_at),
        "failed_at": _ist_iso(failed_at),
        "created_at": _ist_iso(created_at),
    }


# Unset filter fields match every row; untyped NULL parameters need casts.
_SELECTION = (
    "dataset_id=%(dataset_id)s "
    "AND (%(farmer_id)s::text IS NULL OR farmer_id=%(farmer_id)s) "
    "AND (%(crop)s::text IS NULL OR crop=%(crop)s) "
    "AND (%(concern)s::text IS NULL OR concern=%(concern)s) "
    "AND (%(resolution_type)s::text IS NULL OR resolution_type=%(resolution_type)s) "
    "AND (%(quality_flag)s::text IS NULL OR quality_flag=%(quality_flag)s) "
    "AND (%(created_from)s::date IS NULL "
    "OR (created_at AT TIME ZONE 'Asia/Kolkata')::date >= %(created_from)s) "
    "AND (%(created_to)s::date IS NULL "
    "OR (created_at AT TIME ZONE 'Asia/Kolkata')::date <= %(created_to)s)"
)

_RUN_SUMMARY = (
    "SELECT r.run_id,r.status,r.created_at,r.finished_at,r.config_id,r.job_id,"
    "r.manifest->'selection',j.progress,"
    "coalesce(jsonb_object_agg(i.status,i.n) FILTER (WHERE i.status IS NOT NULL),'{}'),"
    "coalesce(sum(i.input_tokens),0),coalesce(sum(i.output_tokens),0) "
    "FROM generation_runs r LEFT JOIN jobs j ON j.job_id=r.job_id "
    "LEFT JOIN (SELECT run_id,status,count(*) n,"
    "sum((outcome->>'input_tokens')::bigint) input_tokens,"
    "sum((outcome->>'output_tokens')::bigint) output_tokens "
    "FROM insight_results GROUP BY run_id,status) i ON i.run_id=r.run_id"
)


def _run_summary(row) -> dict:
    (
        run_id,
        status,
        created,
        finished,
        config_id,
        job_id,
        selection,
        progress,
        counts,
        input_tokens,
        output_tokens,
    ) = row
    return {
        "run_id": str(run_id),
        "status": status,
        "created_at": _iso(created),
        "finished_at": _iso(finished),
        "config_id": str(config_id) if config_id else None,
        "job_id": str(job_id) if job_id else None,
        "selection": selection,
        "progress": progress,
        "counts": counts,
        "input_tokens": int(input_tokens),
        "output_tokens": int(output_tokens),
    }


def _config_summary(row, definition: dict) -> dict:
    config_id, parent_id, status, note, created, digest, name = row
    config = definition["config"]
    return {
        "config_id": str(config_id),
        "name": name,
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
