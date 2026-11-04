import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import RequestUsage

from mel.config import ModelConfig, Settings
from mel.explorer.api import create_app
from mel.store import Store


@pytest.fixture
def stores():
    if not os.environ.get("MEL_TEST_DB_PORT"):
        pytest.skip("Set MEL_TEST_DB_PORT for PostgreSQL usage checks")
    from psycopg import sql

    settings = Settings(
        database_port=int(os.environ["MEL_TEST_DB_PORT"]), database_sslmode="disable"
    )
    root = Store(settings)
    schema = "usage_test_" + uuid4().hex
    root.connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    opened = []

    def factory(_=None):
        db = Store(settings)
        db.connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        db.initialize()
        opened.append(db)
        return db

    try:
        yield factory
    finally:
        for db in opened:
            db.close()
        root.connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
        root.close()


def price(**changes):
    return dict(
        model_id="offline",
        api="responses",
        region="us-east-1",
        input_per_million="2",
        output_per_million="4",
        max_input_tokens=1000,
        source="https://example.test/pricing",
        bound_source="https://example.test/bounds",
        **changes,
    )


def configure(gate, budget="0.01", enabled=True):
    from mel.usage import PolicyUpdate

    return gate.configure(
        PolicyUpdate(
            expected_version=gate.view()["version"],
            enabled=enabled,
            budget_usd=budget,
            prices=[price()],
        )
    )


@contextmanager
def gateway(stores, host=True):
    from mel.usage import UsageGateway

    db = stores()
    yield UsageGateway(db, host_allowed=host)


def reserve(gate):
    from mel.usage import CallIdentity

    return gate.reserve(
        ModelConfig(model_id="offline", max_tokens=100),
        "us-east-1",
        CallIdentity(run_id=str(uuid4()), insight_id="i1", role="generate", attempt=1),
    )


def test_default_policy_persists_off_and_host_override_cannot_enable(stores):
    with gateway(stores) as gate:
        assert gate.view()["effective"] is False
        configure(gate)
    with gateway(stores, host=False) as gate:
        assert gate.view()["enabled"] is True
        assert gate.view()["effective"] is False
        assert "Host has disabled live execution" in gate.view()["blockers"]


def test_money_rejects_nonfinite_negative_and_unproven_prices():
    from mel.usage import PolicyUpdate

    for budget in ["NaN", "Infinity", "-1", "0"]:
        with pytest.raises(ValidationError):
            PolicyUpdate(expected_version=0, enabled=True, budget_usd=budget, prices=[price()])
    invalid = price()
    invalid["source"] = ""
    with pytest.raises(ValidationError):
        PolicyUpdate(expected_version=0, enabled=True, budget_usd="1", prices=[invalid])


def test_settlement_once_uses_snapshot_and_budget_changes_never_reset_spend(stores):
    with gateway(stores) as gate:
        configure(gate)
        call = reserve(gate)
        assert call.reserved_usd == Decimal("0.0024")
        from mel.usage import PolicyUpdate

        new_price = price()
        new_price["input_per_million"] = "20"
        gate.configure(
            PolicyUpdate(expected_version=1, enabled=True, budget_usd="0.02", prices=[new_price])
        )
        gate.settle(call.call_id, 100, 20)
        gate.settle(call.call_id, 999, 99)
        view = gate.view()
        assert Decimal(view["estimated_usd"]) == Decimal("0.00028")
        assert Decimal(view["held_usd"]) == 0
        assert Decimal(view["remaining_usd"]) == Decimal("0.01972")


def test_unknown_keeps_reservation_and_globally_blocks_after_budget_edit(stores):
    from mel.usage import UsageRefused

    with gateway(stores) as gate:
        configure(gate)
        call = reserve(gate)
        gate.settle(call.call_id, 100, None)
        configure(gate, "100")
        assert Decimal(gate.view()["held_usd"]) == Decimal("0.0024")
        assert gate.view()["unresolved_calls"] == 1
        with pytest.raises(UsageRefused):
            reserve(gate)


def test_pending_survives_reconnect_and_prevents_overreservation(stores):
    from mel.usage import UsageRefused

    with gateway(stores) as gate:
        configure(gate, "0.0024")
        reserve(gate)
    with gateway(stores) as gate:
        assert gate.view()["pending_calls"] == 1
        with pytest.raises(UsageRefused):
            reserve(gate)


def test_overrun_records_larger_estimate_and_blocks(stores):
    from mel.usage import UsageRefused

    with gateway(stores) as gate:
        configure(gate)
        call = reserve(gate)
        gate.settle(call.call_id, 3000, 20)
        assert Decimal(gate.view()["estimated_usd"]) == Decimal("0.00608")
        assert gate.view()["unresolved_calls"] == 1
        with pytest.raises(UsageRefused):
            reserve(gate)


def test_concurrent_connections_cannot_overreserve(stores):
    from mel.usage import UsageGateway, UsageRefused

    with gateway(stores) as gate:
        configure(gate, "0.0024")
    gates = [UsageGateway(stores(), host_allowed=True) for _ in range(2)]

    def attempt(gate):
        try:
            return reserve(gate).call_id
        except UsageRefused:
            return None

    with ThreadPoolExecutor(2) as pool:
        result = list(pool.map(attempt, gates))
    assert sum(value is not None for value in result) == 1


def test_stale_policy_cannot_overwrite_newer_configuration(stores):
    from mel.usage import PolicyConflict, PolicyUpdate

    with gateway(stores) as gate:
        configure(gate)
        with pytest.raises(PolicyConflict):
            gate.configure(PolicyUpdate(expected_version=0, enabled=False, budget_usd="1"))
        assert gate.view()["enabled"] is True


def test_guard_counts_each_output_retry_and_stops_after_disable(stores):
    from mel.usage import CallIdentity, MeteredModel, usage_scope

    with gateway(stores) as gate:
        configure(gate, "1")
        calls = []

        async def native(messages, info):
            calls.append(info.model_settings["max_tokens"])
            content = "bad" if len(calls) == 1 else '{"answer":42}'
            return ModelResponse(
                parts=[TextPart(content)], usage=RequestUsage(input_tokens=10, output_tokens=5)
            )

        model = MeteredModel(
            FunctionModel(native),
            gate,
            ModelConfig(model_id="offline", max_tokens=100),
            "us-east-1",
            "generate",
        )
        from pydantic import BaseModel

        class Answer(BaseModel):
            answer: int

        agent = Agent(model, output_type=Answer, retries=1)

        async def run():
            with usage_scope(
                identity=CallIdentity(
                    run_id=str(uuid4()), insight_id="i1", role="generate", attempt=2
                )
            ):
                result = await agent.run("answer")
                assert result.output.answer == 42

        asyncio.run(run())
        assert calls == [100, 100]
        report = gate.analysis()
        assert report["metered"]["calls"] == 2
        assert report["metered"]["input_tokens"] == 20
        assert report["breakdown"][0]["role"] == "generate"
        configure(gate, "1", enabled=False)
        with pytest.raises(Exception, match="disabled"):
            asyncio.run(run())
        assert len(calls) == 2


def test_csrf_and_default_off_admission():
    with TestClient(create_app()) as client:
        assert client.post("/api/experiments", json={}).status_code == 403
        assert (
            client.post(
                "/api/import-recorded",
                headers={"X-MEL-Intent": "operator", "Sec-Fetch-Site": "cross-site"},
                json={},
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/api/import-recorded",
                headers={"X-MEL-Intent": "operator", "Origin": "https://attacker.test"},
                json={},
            ).status_code
            == 403
        )


def test_quote_launch_idempotency_and_changed_terms(stores):
    from test_experiments import specification

    from mel.usage import PolicyUpdate, UsageGateway

    db = stores()
    gate = UsageGateway(db, host_allowed=True)
    spec = specification()
    for candidate in spec.candidates:
        candidate.model = ModelConfig(model_id="offline", max_tokens=100)
    spec.reviewer = ModelConfig(model_id="offline", max_tokens=100)
    configure(gate, "1")
    invoked = []

    async def execute(*args, **kwargs):
        invoked.append(kwargs["experiment_id"])

    headers = {
        "X-MEL-Intent": "operator",
        "X-MEL-Launch-Id": str(uuid4()),
        "X-MEL-Policy-Version": "1",
    }
    with TestClient(
        create_app(store_factory=stores, live_enabled=True, executor=execute)
    ) as client:
        quote = client.post("/api/usage/quote", headers=headers, json=spec.model_dump(mode="json"))
        assert quote.status_code == 200
        assert quote.json()["effective"] is True
        headers["X-MEL-Quote"] = quote.json()["terms_hash"]
        assert quote.json()["requests"][0]["request_reservation_usd"] == "0.0024"
        launch = client.post("/api/experiments", headers=headers, json=spec.model_dump(mode="json"))
        assert launch.status_code == 202
        repeated = client.post(
            "/api/experiments", headers=headers, json=spec.model_dump(mode="json")
        )
        assert repeated.json()["experiment_id"] == launch.json()["experiment_id"]
        assert len(invoked) == 1
        update = PolicyUpdate(expected_version=1, enabled=False, budget_usd="1", prices=[price()])
        assert (
            client.put(
                "/api/live-policy", headers=headers, json=update.model_dump(mode="json")
            ).status_code
            == 200
        )
        headers["X-MEL-Launch-Id"] = str(uuid4())
        assert (
            client.post(
                "/api/experiments", headers=headers, json=spec.model_dump(mode="json")
            ).status_code
            == 409
        )
        assert len(invoked) == 1


def test_unknown_costs_recorded_and_unmetered_runs_remain_separate(stores):
    from mel.models import Outcome

    with gateway(stores) as gate:
        db = gate.store
        for source, complete in [("recorded", False), ("live", True)]:
            key, run = str(uuid4()), str(uuid4())
            db.create_experiment(key, {"name": "saved", "candidates": []}, source=source)
            db.start_run(run, "fixed", {}, purpose="experiment", experiment_id=key)
            db.save(
                run,
                {"insight_id": "i1"},
                Outcome(
                    insight_id="i1",
                    status="failed",
                    input_tokens=13,
                    output_tokens=7,
                    usage_complete=complete,
                ),
            )
        report = gate.analysis()
        assert report["metered"]["calls"] == 0
        assert report["metered"]["estimated_usd"] is None
        assert report["historical"]["recorded"]["input_tokens"] == 13
        assert report["historical"]["recorded"]["complete_outcomes"] == 0
        assert report["historical"]["other"]["complete_outcomes"] == 1
        assert report["historical"]["other"]["estimated_usd"] is None


def test_failure_retains_reservation_and_does_not_start_sibling_call(stores):
    from mel.usage import CallIdentity, MeteredModel, UsageRefused, usage_scope

    with gateway(stores) as gate:
        configure(gate, "1")
        observed = []

        async def native(messages, info):
            observed.append("sent")
            raise RuntimeError("provider failed")

        model = MeteredModel(
            FunctionModel(native),
            gate,
            ModelConfig(model_id="offline", max_tokens=100),
            "us-east-1",
            "generate",
        )

        async def run():
            with usage_scope(identity=CallIdentity(str(uuid4()), "i1", "generate", 1)):
                await Agent(model).run("hello")

        with pytest.raises(UsageRefused):
            asyncio.run(run())
        with pytest.raises(UsageRefused):
            asyncio.run(run())
        assert observed == ["sent"]
        assert gate.view()["unresolved_calls"] == 1
        assert Decimal(gate.view()["held_usd"]) == Decimal("0.0024")


def test_cancellation_holds_request_and_unsupported_stream_never_calls_native(stores):
    from mel.usage import CallIdentity, MeteredModel, UsageRefused, usage_scope

    with gateway(stores) as gate:
        configure(gate, "1")

        async def native(messages, info):
            raise asyncio.CancelledError()

        model = MeteredModel(
            FunctionModel(native),
            gate,
            ModelConfig(model_id="offline", max_tokens=100),
            "us-east-1",
            "generate",
        )

        async def run():
            with usage_scope(identity=CallIdentity(str(uuid4()), "i1", "generate", 1)):
                await Agent(model).run("hello")

        with pytest.raises(asyncio.CancelledError):
            asyncio.run(run())
        assert gate.view()["unresolved_calls"] == 1

        async def stream():
            async with model.request_stream([], None, None):
                pass

        with pytest.raises(UsageRefused, match="Streaming"):
            asyncio.run(stream())


def test_cached_usage_is_unknown_and_never_reported_as_fully_priced(stores):
    from mel.usage import CallIdentity, MeteredModel, UsageRefused, usage_scope

    with gateway(stores) as gate:
        configure(gate, "1")

        async def native(messages, info):
            return ModelResponse(
                parts=[TextPart("hello")],
                usage=RequestUsage(input_tokens=100, output_tokens=20, cache_read_tokens=80),
            )

        model = MeteredModel(
            FunctionModel(native),
            gate,
            ModelConfig(model_id="offline", max_tokens=100),
            "us-east-1",
            "generate",
        )

        async def run():
            with usage_scope(identity=CallIdentity(str(uuid4()), "i1", "generate", 1)):
                await Agent(model).run("hello")

        with pytest.raises(UsageRefused):
            asyncio.run(run())
        report = gate.analysis()["metered"]
        assert report["estimated_usd"] is None
        assert report["known_estimated_usd"] == "0.00028"
        assert report["input_tokens"] == 100
        assert Decimal(gate.view()["held_usd"]) == Decimal("0.0024")


def test_real_experiment_accounts_generation_review_and_scoped_analysis(stores):
    from unittest.mock import patch

    from test_experiments import specification
    from usage_harness import app_for_tests

    with gateway(stores) as gate:
        from mel.usage import PolicyUpdate

        sourced = price()
        sourced["max_input_tokens"] = 10000
        gate.configure(
            PolicyUpdate(expected_version=0, enabled=True, budget_usd="1", prices=[sourced])
        )
    spec = specification()
    for candidate in spec.candidates:
        candidate.model = ModelConfig(model_id="offline", max_tokens=1800)
    spec.reviewer = ModelConfig(model_id="offline", max_tokens=1800)
    headers = {
        "X-MEL-Intent": "operator",
        "X-MEL-Launch-Id": str(uuid4()),
        "X-MEL-Policy-Version": "1",
    }
    with (
        patch("boto3.client", side_effect=AssertionError("AWS forbidden")),
        patch("boto3.Session", side_effect=AssertionError("AWS forbidden")),
    ):
        with TestClient(app_for_tests(stores)) as client:
            quote = client.post(
                "/api/usage/quote", headers=headers, json=spec.model_dump(mode="json")
            )
            headers["X-MEL-Quote"] = quote.json()["terms_hash"]
            launch = client.post(
                "/api/experiments", headers=headers, json=spec.model_dump(mode="json")
            )
            assert launch.status_code == 202
            key = launch.json()["experiment_id"]
            import time

            for _ in range(100):
                detail = client.get(f"/api/experiments/{key}").json()
                if detail["status"] not in ("queued", "running"):
                    break
                time.sleep(0.01)
            assert detail["status"] == "completed"
            report = client.get(f"/api/usage?experiment_id={key}").json()
            assert report["metered"]["calls"] == 4
            assert {row["role"] for row in report["breakdown"]} == {"generate", "review"}
            assert report["metered"]["usage_complete"] is True
            run = detail["runs"][0]["run_id"]
            selected = client.get(f"/api/usage?run_id={run}").json()
            assert selected["metered"]["calls"] == 2
            assert all(call["run_id"] == run for call in selected["calls"])
            assert all(call["attempt"] == 1 for call in selected["calls"])


def test_mid_run_policy_edit_stops_next_native_request(stores):
    from mel.usage import CallIdentity, MeteredModel, PolicyConflict, usage_scope

    with gateway(stores) as gate:
        configure(gate, "1")
        gate.expected_version = 1

        async def native(messages, info):
            configure(gate, "1", enabled=False)
            return ModelResponse(
                parts=[TextPart("done")], usage=RequestUsage(input_tokens=10, output_tokens=5)
            )

        model = MeteredModel(
            FunctionModel(native),
            gate,
            ModelConfig(model_id="offline", max_tokens=100),
            "us-east-1",
            "generate",
        )

        async def run():
            with usage_scope(identity=CallIdentity(str(uuid4()), "i1", "generate", 1)):
                await Agent(model).run("hello")

        asyncio.run(run())
        with pytest.raises(PolicyConflict):
            asyncio.run(run())
        assert gate.analysis()["metered"]["calls"] == 1


def test_website_native_mantle_has_no_hidden_retries_and_enforces_wire_limit(stores, monkeypatch):
    import json
    from unittest.mock import Mock

    import httpx
    from botocore.credentials import Credentials

    from mel.agents import build_agents
    from mel.bedrock import create_mantle_client
    from mel.bundle import Bundle
    from mel.usage import CallIdentity, UsageRefused, usage_scope
    from mel.worker import close_model_clients

    with gateway(stores) as gate:
        configure(gate, "1")
        observed = []
        session = Mock()
        session.get_credentials.return_value = Credentials("test-access", "test-secret")
        monkeypatch.setattr("mel.bedrock.boto3.Session", lambda **_: session)

        def response(request):
            observed.append(json.loads(request.content))
            return httpx.Response(
                500, json={"error": {"message": "offline failure", "type": "server_error"}}
            )

        clients = []

        def client_factory(region, project, **kwargs):
            client = create_mantle_client(
                region, project, transport=httpx.MockTransport(response), **kwargs
            )
            clients.append(client)
            return client

        monkeypatch.setattr("mel.agents.create_mantle_client", client_factory)
        bundle = Bundle.load(Settings(bedrock_model_id="offline"))
        bundle.config.generation.max_tokens = 100

        async def run():
            with usage_scope(
                gateway=gate, identity=CallIdentity(str(uuid4()), "i1", "generate", 1)
            ):
                agents = build_agents(bundle.config, "us-east-1", bundle.snapshot["prompts"])
                try:
                    await agents[0].run("offline")
                finally:
                    await close_model_clients(*agents)

        with pytest.raises(UsageRefused):
            asyncio.run(run())
        assert len(observed) == 1
        assert observed[0]["max_output_tokens"] == 100
        assert all(client.is_closed() for client in clients)
        assert gate.view()["unresolved_calls"] == 1


def test_website_converse_passes_explicit_single_attempt_transport(stores, monkeypatch):
    from types import SimpleNamespace

    from mel.agents import build_agents
    from mel.bundle import Bundle
    from mel.usage import usage_scope
    from mel.worker import close_model_clients

    received = []

    class OfflineClient:
        meta = SimpleNamespace(endpoint_url="https://offline.invalid", region_name="us-east-1")

        def close(self):
            pass

        def converse(self, **kwargs):
            raise AssertionError("Native calls are forbidden in this constructor check")

    def client(service, **kwargs):
        received.append((service, kwargs["config"].retries))
        return OfflineClient()

    monkeypatch.setattr("boto3.client", client)
    bundle = Bundle.load(Settings(bedrock_model_id="offline"))
    bundle.config.generation.api = bundle.config.review.api = "converse"
    with gateway(stores) as gate:
        with usage_scope(gateway=gate):
            agents = build_agents(bundle.config, "us-east-1", bundle.snapshot["prompts"])
        assert received == [("bedrock-runtime", {"total_max_attempts": 1, "mode": "standard"})]
        asyncio.run(close_model_clients(*agents))


def test_overrun_response_stops_pipeline_immediately(stores):
    from mel.usage import CallIdentity, MeteredModel, UsageRefused, usage_scope

    with gateway(stores) as gate:
        configure(gate, "1")

        async def native(messages, info):
            return ModelResponse(
                parts=[TextPart("done")], usage=RequestUsage(input_tokens=10, output_tokens=101)
            )

        model = MeteredModel(
            FunctionModel(native),
            gate,
            ModelConfig(model_id="offline", max_tokens=100),
            "us-east-1",
            "generate",
        )

        async def run():
            with usage_scope(identity=CallIdentity(str(uuid4()), "i1", "generate", 1)):
                await Agent(model).run("hello")

        with pytest.raises(UsageRefused):
            asyncio.run(run())
        assert gate.view()["unresolved_calls"] == 1


def test_quote_terms_reject_changed_request_configuration(stores):
    from test_experiments import specification

    from mel.usage import PolicyConflict

    with gateway(stores) as gate:
        configure(gate, "1")
        spec = specification()
        for candidate in spec.candidates:
            candidate.model = ModelConfig(model_id="offline", max_tokens=100)
        spec.reviewer = ModelConfig(model_id="offline", max_tokens=100)
        from mel.config import GraphPolicy

        graph = GraphPolicy()
        quote = gate.quote(spec, "us-east-1", graph)
        spec.candidates[0].model.max_tokens = 101
        with pytest.raises(PolicyConflict):
            gate.launch(str(uuid4()), spec, 1, "us-east-1", graph, quote_hash=quote["terms_hash"])
