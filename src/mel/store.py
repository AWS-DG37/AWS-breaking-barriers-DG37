"""PostgreSQL checkpoints and transactional scheduler outbox.

One worker owns a run (session advisory lock). Its bounded coroutines serialize
short DB operations through this connection; model calls remain concurrent.
"""

import json
from pathlib import Path
from threading import RLock

import boto3

from mel.config import Settings
from mel.models import Outcome


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
            self.connection.execute(Path("migrations/001_initial.sql").read_text())

    def start_run(self, run_id: str, fingerprint: str, manifest: dict) -> None:
        from psycopg.types.json import Jsonb

        with self.lock:
            locked = self.connection.execute(
                "SELECT pg_try_advisory_lock(hashtextextended(%s, 0))", (run_id,)
            ).fetchone()[0]
            if not locked:
                raise ValueError("another worker owns this run_id")
            with self.connection.transaction():
                self.connection.execute(
                    "INSERT INTO generation_runs(run_id,fingerprint,manifest) VALUES (%s,%s,%s) "
                    "ON CONFLICT (run_id) DO NOTHING",
                    (run_id, fingerprint, Jsonb(manifest)),
                )
                previous = self.connection.execute(
                    "SELECT fingerprint FROM generation_runs WHERE run_id=%s", (run_id,)
                ).fetchone()[0]
                if previous != fingerprint:
                    raise ValueError("run_id exists with different input/config; use a new ID")
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
            if outcome.status == "accepted":
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

    def close(self) -> None:
        self.connection.close()
