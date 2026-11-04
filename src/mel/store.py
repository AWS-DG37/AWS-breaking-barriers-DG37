"""PostgreSQL checkpoints and transactional scheduler outbox.

One worker owns a run (session advisory lock). Its bounded coroutines serialize
short DB operations through this connection; model calls remain concurrent.
"""

import json
from pathlib import Path
from threading import RLock
from typing import Literal

import boto3

from mel.config import Settings
from mel.models import Outcome

RunPurpose = Literal["handoff", "experiment"]


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
            for migration in sorted(Path("migrations").glob("*.sql")):
                self.connection.execute(migration.read_text())

    def start_run(
        self,
        run_id: str,
        fingerprint: str,
        manifest: dict,
        *,
        purpose: RunPurpose = "handoff",
        experiment_id: str | None = None,
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
                    "(run_id,fingerprint,manifest,purpose,experiment_id) "
                    "VALUES (%s,%s,%s,%s,%s) "
                    "ON CONFLICT (run_id) DO NOTHING",
                    (run_id, fingerprint, Jsonb(manifest), purpose, experiment_id),
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

    def close(self) -> None:
        self.connection.close()
