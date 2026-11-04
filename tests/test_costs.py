"""Cost calculations and request-wrapper behavior, without AWS or PostgreSQL."""

import asyncio
import json
from contextlib import nullcontext
from datetime import date
from decimal import Decimal
from threading import RLock
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import RequestUsage

from mel.config import ModelConfig
from mel.costs import CostedModel, CostRefused, Price, RunCosts, request_scope


def price(**changes):
    return Price.model_validate(
        dict(
            input_per_million="1",
            output_per_million="5",
            cache_read_per_million="0.1",
            max_input_tokens=10000,
            checked_on=date(2026, 11, 4),
            source="https://example.com/prices",
        )
        | changes
    )


def test_costs_count_cached_input_once_and_use_the_full_request_context_tier():
    rates = price(
        long_context_threshold=5000,
        long_input_per_million="2",
        long_output_per_million="8",
        long_cache_read_per_million="0.2",
    )
    assert rates.estimate(1000, 100, 200) == Decimal("0.00132")
    assert rates.estimate(6000, 100, 200) == Decimal("0.01244")
    assert rates.reservation(100) == Decimal("0.0208")
    with pytest.raises(ValueError, match="Invalid"):
        rates.estimate(1, 2, 3)
    with pytest.raises(ValueError, match="no verified price"):
        price(cache_read_per_million=None).estimate(1, 2, 1)


@pytest.mark.parametrize(
    "changes",
    [
        dict(input_per_million="NaN"),
        dict(output_per_million="Infinity"),
        dict(input_per_million="-1"),
        dict(max_input_tokens=0),
        dict(long_context_threshold=1),
    ],
)
def test_invalid_prices_cannot_enter_the_catalog(changes):
    with pytest.raises(ValidationError):
        price(**changes)


@pytest.mark.parametrize("inputs,outputs", [(1000, 100), (0, 0)])
def test_settlement_serializes_native_usage_and_preserves_unknown_counts(inputs, outputs):
    # Exercise real settlement with the installed SDK type. Wrapper-only ledger
    # doubles cannot catch serialization failures at this persistence boundary.
    class Connection:
        def transaction(self):
            return nullcontext()

        def execute(self, query, params):
            if query.startswith("SELECT budget_usd"):
                return SimpleNamespace(fetchone=lambda: (Decimal("5"),))
            if query.startswith("SELECT c.state"):
                return SimpleNamespace(
                    fetchone=lambda: (
                        "pending", Decimal("0.0105"), 100, price().model_dump(mode="json")
                    )
                )
            if query.startswith("UPDATE model_calls"):
                self.saved = params
            elif query.startswith("UPDATE run_costs"):
                self.stopped = params[0]
            else:
                pytest.fail(f"Unexpected settlement query: {query}")

    connection = Connection()
    costs = RunCosts(SimpleNamespace(connection=connection, lock=RLock()), "run")
    usage = RequestUsage(
        input_tokens=inputs, output_tokens=outputs, details={"reasoning_tokens": 2}
    )
    response = ModelResponse([TextPart("ok")], usage=usage, provider_response_id="provider-request")
    expected = "complete" if inputs else "unknown"
    assert costs.settle("call", response) == expected
    (state, saved_inputs, saved_outputs, estimate,
     saved_usage, request_id, error, key) = connection.saved
    assert state == expected
    assert (saved_inputs, saved_outputs) == ((inputs, outputs) if inputs else (None, None))
    assert estimate == (Decimal("0.0015") if inputs else None)
    # Validate the JSON payload supplied to psycopg, including provider details.
    payload = json.loads(json.dumps(saved_usage.obj))
    assert payload["input_tokens"] == inputs
    assert payload["output_tokens"] == outputs
    assert payload["details"] == {"reasoning_tokens": 2}
    assert (request_id, error, key) == ("provider-request", None, "call")
    if not inputs:
        assert connection.stopped == "Unresolved request costs; this run cannot send more requests"


class Ledger:
    """A boundary double: asserts provider calls are surrounded by ledger actions."""

    def __init__(self, refuse=False):
        self.events = []
        self.refuse = refuse

    def reserve(self, model, region, role, attempt, insight_id):
        self.events.append(("reserve", role, attempt, insight_id))
        if self.refuse:
            raise CostRefused("Run budget cannot cover the next request")
        return "call"

    def settle(self, key, response=None, error=None):
        self.events.append(("settle", key, response.usage if response else None, error))
        return "complete"

    def stop(self, reason):
        self.events.append(("stop", reason))


def model(ledger, function):
    return CostedModel(
        FunctionModel(function),
        ledger,
        ModelConfig(model_id="test", max_tokens=321),
        "us-east-1",
        "generate",
    )


def test_wrapper_reserves_each_native_call_and_keeps_insight_attempt_identity():
    ledger = Ledger()

    def respond(messages, info):
        assert ledger.events[-1][0] == "reserve"
        assert info.model_settings["max_tokens"] == 321
        return ModelResponse([TextPart("ok")], usage=RequestUsage(input_tokens=10, output_tokens=2))

    wrapped = model(ledger, respond)

    async def run():
        for attempt in (1, 2):
            with request_scope("insight", attempt):
                await wrapped.request([], {"max_tokens": 999}, ModelRequestParameters())

    asyncio.run(run())
    assert [e for e in ledger.events if e[0] == "reserve"] == [
        ("reserve", "generate", 1, "insight"),
        ("reserve", "generate", 2, "insight"),
    ]
    assert len([e for e in ledger.events if e[0] == "settle"]) == 2


def test_budget_refusal_never_reaches_the_provider():
    ledger = Ledger(refuse=True)

    def forbidden(*args):
        pytest.fail("Budget refusal must happen before the provider call")

    async def run():
        with request_scope("insight"):
            await model(ledger, forbidden).request([], {}, ModelRequestParameters())

    with pytest.raises(CostRefused, match="budget"):
        asyncio.run(run())
    assert ledger.events[-1][0] == "stop"


def test_provider_failure_and_cancellation_settle_as_unknown_before_propagating():
    for error in (RuntimeError("failed"), asyncio.CancelledError()):
        ledger = Ledger()

        async def fail(*args):
            raise error

        async def run():
            await model(ledger, fail).request([], {}, ModelRequestParameters())

        with pytest.raises(type(error)):
            asyncio.run(run())
        assert ledger.events[-1] == ("settle", "call", None, type(error).__name__)


def test_request_identity_is_isolated_across_concurrent_insights():
    ledger = Ledger()

    def respond(*args):
        return ModelResponse([TextPart("ok")], usage=RequestUsage(input_tokens=10, output_tokens=2))

    wrapped = model(ledger, respond)

    async def one(key):
        with request_scope(key):
            await asyncio.sleep(0)
            await wrapped.request([], {}, ModelRequestParameters())

    async def run():
        await asyncio.gather(one("first"), one("second"))

    asyncio.run(run())
    assert {e[3] for e in ledger.events if e[0] == "reserve"} == {"first", "second"}


def test_cache_write_rates_are_separate_and_reservations_cover_the_highest_input_rate():
    rates = price(cache_write_per_million="2")
    assert rates.estimate(1000, 100, cached=100, writes=200) == Decimal("0.00161")
    assert rates.reservation(100) == Decimal("0.0205")
    with pytest.raises(ValueError, match="Cache writes"):
        price().estimate(1000, 100, writes=1)


def test_api_budget_validation_and_queue_persistence():
    from fastapi.testclient import TestClient
    from test_explorer_api import DATASET
    from test_explorer_api import Store as MemoryStore

    from mel.explorer.api import create_app

    store = MemoryStore()
    with TestClient(create_app(store_factory=lambda _: store)) as client:
        # Establish a runnable version using the normal API path.
        client.get("/api/config")
        for budget in (0, -1, "NaN", "Infinity"):
            response = client.post(
                "/api/jobs/batch", json={"dataset_id": DATASET, "budget_usd": budget}
            )
            assert response.status_code == 422
        response = client.post(
            "/api/jobs/batch", json={"dataset_id": DATASET, "budget_usd": "2.50"}
        )
        assert response.status_code == 202
        assert Decimal(store.jobs[-1][1]["budget_usd"]) == Decimal("2.50")


def test_settlement_storage_failure_stops_execution_even_when_stop_cannot_be_saved():
    ledger = Ledger()

    def failed_storage(*args, **kwargs):
        raise RuntimeError("Database unavailable")

    ledger.settle = ledger.stop = failed_storage

    def respond(*args):
        return ModelResponse([TextPart("ok")], usage=RequestUsage(input_tokens=10, output_tokens=2))

    async def run():
        await model(ledger, respond).request([], {}, ModelRequestParameters())

    with pytest.raises(CostRefused, match="accounting failed"):
        asyncio.run(run())
