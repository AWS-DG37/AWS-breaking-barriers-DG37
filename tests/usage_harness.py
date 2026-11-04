"""Offline Explorer backed by PostgreSQL. Native AWS clients are forbidden."""

import argparse
import json
import os
from pathlib import Path
from unittest.mock import patch

import uvicorn
from psycopg import sql
from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel

from mel.bedrock_models import ModelCandidate
from mel.config import Settings
from mel.experiments import execute_experiment
from mel.explorer.api import create_app
from mel.models import Review, SurveyDraft
from mel.store import Store
from mel.usage import MeteredModel, gateway_context


def offline_agents(config, region, prompts, project_id):
    survey = json.loads(
        Path(
            "docs/participant-pack/json/scenarios/1-mustard-aphid-spray/survey.hi-IN.json"
        ).read_text()
    )
    outputs = [
        {"items": survey["items"], "rationale": survey["rationale"]},
        {
            "relevance_score": 0.95,
            "language_correct": True,
            "gives_new_advice": False,
            "asks_personal_info": False,
            "issues": [],
        },
    ]
    agents = []
    for role, model_config, output_type, output in zip(
        ("generate", "review"), (config.generation, config.review), (SurveyDraft, Review), outputs
    ):
        native = TestModel(custom_output_args=output)
        gate = gateway_context.get()
        if gate is None:
            raise RuntimeError("Offline website must still use the real usage gateway")
        agents.append(
            Agent(
                MeteredModel(native, gate, model_config, region, role),
                output_type=output_type,
                retries=config.graph.agent_output_retries,
            )
        )
    return tuple(agents)


async def offline_execute(*args, **kwargs):
    return await execute_experiment(*args, **kwargs, agent_factory=offline_agents)


async def offline_catalog(region):
    return [
        ModelCandidate(
            model_id="offline",
            name="Offline verification model",
            provider="Test only",
            api="responses",
            status="verified",
            reason="Local Pydantic model. No AWS access.",
        )
    ]


def app_for_tests(store_factory=Store, *, port=18877):
    settings = Settings(
        database_port=int(os.environ.get("MEL_TEST_DB_PORT", "55438")),
        database_sslmode="disable",
        bedrock_model_id="offline",
        explorer_allowed_origins=[f"http://127.0.0.1:{port}", f"http://localhost:{port}"],
    )
    return create_app(
        settings=settings,
        store_factory=store_factory,
        executor=offline_execute,
        catalog_loader=offline_catalog,
        live_enabled=True,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=18877)
    parser.add_argument("--schema", default="usage_browser_verification")
    args = parser.parse_args()
    if not args.schema.startswith("usage_browser_"):
        parser.error("Use a dedicated usage_browser_ schema")

    def store(settings):
        db = Store(settings)
        db.connection.execute(
            sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(args.schema))
        )
        db.connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(args.schema)))
        return db

    def forbidden(*args, **kwargs):
        raise AssertionError("AWS client construction is forbidden in the offline harness")

    with (
        patch("boto3.client", forbidden),
        patch("boto3.Session", forbidden),
        patch("mel.agents.create_mantle_client", forbidden),
        patch("mel.agents.BedrockProvider", forbidden),
    ):
        uvicorn.run(app_for_tests(store, port=args.port), host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
