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

from mel.browse import ResultFilter, RunFilter, like_pattern
from mel.bundle import definition_sha256
from mel.config import Settings
from mel.datasets import InsightFilter, index_columns
from mel.models import Outcome

RunPurpose = Literal["handoff", "experiment"]
JobKind = Literal["batch", "experiment", "improve"]
ImprovementStatus = Literal["completed", "failed"]
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

    def list_runs(self, runs: RunFilter, limit: int = 200) -> list[dict]:
        with self.lock:
            rows = self.connection.execute(
                f"{_RUN_SUMMARY} WHERE r.purpose='handoff' "
                "AND (%(status)s::text IS NULL OR r.status=%(status)s) "
                "AND (%(config)s::uuid IS NULL OR r.config_id=%(config)s) "
                f"GROUP BY r.run_id,j.job_id ORDER BY {_RUN_ORDER[runs.sort]} LIMIT %(limit)s",
                {"status": runs.status, "config": runs.config_id, "limit": limit},
            ).fetchall()
        return [_run_summary(row) for row in rows]

    def run_summary(self, run_id: str) -> dict | None:
        with self.lock:
            row = self.connection.execute(
                f"{_RUN_SUMMARY} WHERE r.run_id=%s GROUP BY r.run_id,j.job_id", (run_id,)
            ).fetchone()
        return _run_summary(row) if row else None

    def run_results(
        self, run_id: str, results: ResultFilter, offset: int = 0, limit: int = 50
    ) -> list[dict]:
        """One page of matching results, with the fields a results table shows. The
        full outcome comes from run_result."""
        search = like_pattern(results.search) if results.search else None
        with self.lock:
            rows = self.connection.execute(
                "SELECT insight_id,status,insight,outcome->>'rule',outcome->>'reason',"
                "outcome->>'detail',outcome->'survey'->'items'->0->>'prompt',"
                "outcome->'candidate_survey'->'items'->0->>'prompt',"
                "jsonb_array_length(coalesce(outcome->'survey'->'items','[]')),"
                "(outcome->'review'->>'relevance_score')::float,"
                "(outcome->>'elapsed_seconds')::float FROM insight_results "
                "WHERE run_id=%(run)s AND (%(status)s::text IS NULL OR status=%(status)s) "
                "AND (%(crop)s::text IS NULL OR insight->'asset'->>'name'=%(crop)s) "
                "AND (%(concern)s::text IS NULL OR insight->>'concern'=%(concern)s) "
                "AND (%(rule)s::text IS NULL OR outcome->>'rule'=%(rule)s) "
                "AND (%(search)s::text IS NULL OR insight_id ILIKE %(search)s "
                "OR insight->>'query_summary' ILIKE %(search)s "
                "OR insight->>'advice_summary' ILIKE %(search)s "
                "OR EXISTS (SELECT 1 FROM jsonb_array_elements(coalesce(outcome->'survey'->"
                "'items',outcome->'candidate_survey'->'items','[]')) q "
                "WHERE q->>'prompt' ILIKE %(search)s)) "
                f"ORDER BY {_RESULT_ORDER[results.sort]} {results.direction.upper()} NULLS LAST,"
                f"insight_id {results.direction.upper()} "
                "LIMIT %(limit)s OFFSET %(offset)s",
                {
                    "run": run_id,
                    "status": results.status,
                    "crop": results.crop,
                    "concern": results.concern,
                    "rule": results.rule,
                    "search": search,
                    "limit": limit,
                    "offset": offset,
                },
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

    def run_facets(self, run_id: str) -> dict:
        """The crops, concerns and deciding rules in a run, with result counts."""
        columns = {
            "crops": "insight->'asset'->>'name'",
            "concerns": "insight->>'concern'",
            "rules": "outcome->>'rule'",
        }
        with self.lock:
            return {
                key: dict(
                    self.connection.execute(
                        f"SELECT {column},count(*) FROM insight_results WHERE run_id=%s "
                        f"AND {column} IS NOT NULL GROUP BY 1 ORDER BY 2 DESC,1",
                        (run_id,),
                    ).fetchall()
                )
                for key, column in columns.items()
            }

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

    def delete_run(self, run_id: str) -> dict | None:
        """Delete a finished Runs-tab run with its results, request records, pending
        handoffs and, when nothing else uses it, its batch job. The run's capped spend
        moves to spend_cap.deleted_usd; review comments keep their snapshot."""
        from mel.costs import CALL_EXPOSURE

        with self.lock, self.connection.transaction():
            row = self.connection.execute(
                "SELECT r.purpose,r.status,r.job_id,j.status FROM generation_runs r "
                "LEFT JOIN jobs j ON j.job_id=r.job_id WHERE r.run_id=%s FOR UPDATE OF r",
                (run_id,),
            ).fetchone()
            if row is None:
                return None
            purpose, status, job_id, job_status = row
            if purpose != "handoff":
                raise ValueError("this run belongs to a comparison; it cannot be deleted alone")
            if job_status in ("queued", "running") or (job_id is None and status == "running"):
                raise ValueError("the run is still running; wait for it to finish")
            claimed = self.connection.execute(
                "SELECT count(*) FROM survey_handoffs WHERE run_id=%s AND status<>'pending'",
                (run_id,),
            ).fetchone()[0]
            if claimed:
                raise ValueError(
                    f"{claimed} of its surveys were already taken by the scheduler; "
                    "the run cannot be deleted"
                )
            # Lock order is spend_cap, then run_costs, as in request reservations.
            self.connection.execute("SELECT 1 FROM spend_cap FOR UPDATE")
            self.connection.execute(
                "UPDATE spend_cap SET deleted_usd=deleted_usd+("
                f"SELECT {CALL_EXPOSURE} FROM model_calls c JOIN run_costs r USING(run_id) "
                "WHERE r.capped AND c.run_id=%s)",
                (run_id,),
            )
            counts = {
                table: self.connection.execute(
                    f"DELETE FROM {table} WHERE run_id=%s", (run_id,)
                ).rowcount
                for table in ("model_calls", "run_costs", "survey_handoffs", "insight_results")
            }
            detached = self.connection.execute(
                "SELECT count(*) FROM review_items WHERE run_id=%s", (run_id,)
            ).fetchone()[0]
            self.connection.execute("DELETE FROM generation_runs WHERE run_id=%s", (run_id,))
            job_deleted = job_id is not None and bool(
                self.connection.execute(
                    "DELETE FROM jobs WHERE job_id=%s AND kind='batch' AND NOT EXISTS "
                    "(SELECT 1 FROM generation_runs WHERE job_id=%s)",
                    (job_id, job_id),
                ).rowcount
            )
        return {
            "run_id": run_id,
            "results": counts["insight_results"],
            "handoffs": counts["survey_handoffs"],
            "model_calls": counts["model_calls"],
            "review_comments_kept": detached,
            "job_deleted": job_deleted,
        }

    # Review lists. Items snapshot the insight and survey they comment on.

    def create_review_list(self, name: str, description: str | None = None) -> dict:
        key = str(uuid4())
        with self.lock:
            self.connection.execute(
                "INSERT INTO review_lists(list_id,name,description) VALUES (%s,%s,%s)",
                (key, name, description),
            )
        return self.review_list(key)

    def list_review_lists(self) -> list[dict]:
        with self.lock:
            rows = self.connection.execute(
                f"{_REVIEW_LIST} GROUP BY l.list_id ORDER BY l.created_at DESC"
            ).fetchall()
        return [_review_list_summary(row) for row in rows]

    def review_list(self, list_id: str) -> dict | None:
        with self.lock:
            row = self.connection.execute(
                f"{_REVIEW_LIST} WHERE l.list_id=%s GROUP BY l.list_id", (list_id,)
            ).fetchone()
        return _review_list_summary(row) if row else None

    def review_items(self, list_id: str) -> list[dict]:
        with self.lock:
            rows = self.connection.execute(
                f"SELECT {_REVIEW_ITEM_COLUMNS} FROM review_items "
                "WHERE list_id=%s ORDER BY created_at,item_id",
                (list_id,),
            ).fetchall()
        return [_review_item(row) for row in rows]

    def save_review_item(
        self,
        list_id: str,
        *,
        run_id: str,
        insight_id: str,
        question_id: str | None,
        comment: str,
        should_be: str | None,
        config_id: str | None,
        insight: dict,
        survey: dict,
        status: str,
    ) -> dict:
        """Add a comment, or replace the comment already on the same question."""
        from psycopg.types.json import Jsonb

        with self.lock:
            row = self.connection.execute(
                "INSERT INTO review_items(item_id,list_id,run_id,insight_id,question_id,comment,"
                "should_be,config_id,insight,survey,status) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (list_id,run_id,insight_id,coalesce(question_id,'')) DO UPDATE "
                "SET comment=EXCLUDED.comment,should_be=EXCLUDED.should_be,updated_at=now() "
                f"RETURNING {_REVIEW_ITEM_COLUMNS}",
                (
                    str(uuid4()),
                    list_id,
                    run_id,
                    insight_id,
                    question_id,
                    comment,
                    should_be,
                    config_id,
                    Jsonb(insight),
                    Jsonb(survey),
                    status,
                ),
            ).fetchone()
        return _review_item(row)

    def update_review_item(self, item_id: str, comment: str, should_be: str | None) -> dict | None:
        with self.lock:
            row = self.connection.execute(
                "UPDATE review_items SET comment=%s,should_be=%s,updated_at=now() "
                f"WHERE item_id=%s RETURNING {_REVIEW_ITEM_COLUMNS}",
                (comment, should_be, item_id),
            ).fetchone()
        return _review_item(row) if row else None

    def delete_review_item(self, item_id: str) -> bool:
        with self.lock:
            row = self.connection.execute(
                "DELETE FROM review_items WHERE item_id=%s RETURNING item_id", (item_id,)
            ).fetchone()
        return row is not None

    # Improvement loops. Rounds are appended as each finishes, so a page can follow one.

    def create_improvement(
        self, improvement_id: str, list_id: str, spec: dict, job_id: str
    ) -> None:
        from psycopg.types.json import Jsonb

        with self.lock:
            self.connection.execute(
                "INSERT INTO improvements(improvement_id,list_id,spec,job_id) VALUES (%s,%s,%s,%s)",
                (improvement_id, list_id, Jsonb(spec), job_id),
            )

    def start_improvement(self, improvement_id: str) -> None:
        """Mark it running and clear rounds a stopped attempt left, so a rerun starts over."""
        with self.lock:
            self.connection.execute(
                "UPDATE improvements SET status='running',rounds='[]',verdict=NULL,"
                "best_config_id=NULL,error=NULL,finished_at=NULL WHERE improvement_id=%s",
                (improvement_id,),
            )

    def add_improvement_round(self, improvement_id: str, round_: dict) -> None:
        from psycopg.types.json import Jsonb

        with self.lock:
            self.connection.execute(
                "UPDATE improvements SET rounds=rounds||jsonb_build_array(%s::jsonb) "
                "WHERE improvement_id=%s",
                (Jsonb(round_), improvement_id),
            )

    def finish_improvement(
        self,
        improvement_id: str,
        status: ImprovementStatus,
        *,
        verdict: dict | None = None,
        best_config_id: str | None = None,
        error: str | None = None,
    ) -> None:
        from psycopg.types.json import Jsonb

        with self.lock:
            self.connection.execute(
                "UPDATE improvements SET status=%s,verdict=%s,best_config_id=%s,error=%s,"
                "finished_at=now() WHERE improvement_id=%s",
                (
                    status,
                    Jsonb(verdict) if verdict is not None else None,
                    best_config_id,
                    error,
                    improvement_id,
                ),
            )

    def improvement(self, improvement_id: str) -> dict | None:
        with self.lock:
            row = self.connection.execute(
                f"SELECT {_IMPROVEMENT_COLUMNS} FROM improvements WHERE improvement_id=%s",
                (improvement_id,),
            ).fetchone()
        return _improvement(row) if row else None

    def list_improvements(self, list_id: str) -> list[dict]:
        with self.lock:
            rows = self.connection.execute(
                f"SELECT {_IMPROVEMENT_COLUMNS} FROM improvements "
                "WHERE list_id=%s ORDER BY created_at DESC",
                (list_id,),
            ).fetchall()
        return [_improvement(row) for row in rows]

    def close(self) -> None:
        self.connection.close()


def _iso(value) -> str | None:
    return value.isoformat() if value else None


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


# Whitelisted ORDER BY clauses; filters choose a key, never SQL.
_RUN_ORDER = {
    "newest": "r.created_at DESC,r.run_id DESC",
    "oldest": "r.created_at,r.run_id",
    "most_accepted": "coalesce(sum(i.n) FILTER (WHERE i.status='accepted'),0) DESC,"
    "r.created_at DESC,r.run_id DESC",
    "most_results": "coalesce(sum(i.n),0) DESC,r.created_at DESC,r.run_id DESC",
}
_RESULT_ORDER = {
    "processed": "updated_at",
    "insight": "insight_id",
    # ISO text, not a cast: junk insights (R1) may carry a malformed date.
    "insight_date": "insight->>'created_at'",
    "outcome": "array_position(ARRAY['accepted','rejected','skipped','failed'],status)",
    "relevance": "(outcome->'review'->>'relevance_score')::float",
    "time": "(outcome->>'elapsed_seconds')::float",
}


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


_REVIEW_LIST = (
    "SELECT l.list_id,l.name,l.description,l.created_at,count(i.item_id),"
    "count(DISTINCT (i.run_id,i.insight_id)) FROM review_lists l "
    "LEFT JOIN review_items i ON i.list_id=l.list_id"
)


def _review_list_summary(row) -> dict:
    list_id, name, description, created, items, insights = row
    return {
        "list_id": str(list_id),
        "name": name,
        "description": description,
        "created_at": _iso(created),
        "item_count": items,
        "insight_count": insights,
    }


_REVIEW_ITEM_COLUMNS = (
    "item_id,list_id,run_id,insight_id,question_id,comment,should_be,config_id,"
    "insight,survey,status,created_at,updated_at"
)


def _review_item(row) -> dict:
    (
        item_id,
        list_id,
        run_id,
        insight_id,
        question_id,
        comment,
        should_be,
        config_id,
        insight,
        survey,
        status,
        created,
        updated,
    ) = row
    return {
        "item_id": str(item_id),
        "list_id": str(list_id),
        "run_id": str(run_id) if run_id else None,
        "insight_id": insight_id,
        "question_id": question_id,
        "comment": comment,
        "should_be": should_be,
        "config_id": str(config_id) if config_id else None,
        "insight": insight,
        "survey": survey,
        "status": status,
        "created_at": _iso(created),
        "updated_at": _iso(updated),
    }


_IMPROVEMENT_COLUMNS = (
    "improvement_id,list_id,job_id,spec,status,rounds,verdict,best_config_id,error,"
    "created_at,finished_at"
)


def _improvement(row) -> dict:
    key, list_id, job_id, spec, status, rounds, verdict, best, error, created, finished = row
    return {
        "improvement_id": str(key),
        "list_id": str(list_id),
        "job_id": str(job_id),
        "spec": spec,
        "status": status,
        "rounds": rounds,
        "verdict": verdict,
        "best_config_id": str(best) if best else None,
        "error": error,
        "created_at": _iso(created),
        "finished_at": _iso(finished),
    }
