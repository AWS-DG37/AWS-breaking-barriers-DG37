"""Ledger persistence and budget enforcement against real PostgreSQL."""

from decimal import Decimal
from uuid import uuid4

import pytest
from postgres import requires_postgres
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.usage import RequestUsage
from test_costs import price

from mel.config import ModelConfig
from mel.costs import CostRefused, NewPrice, RunCosts, report, save_price

pytestmark = requires_postgres


def setup_run(db, budget="0.02"):
    key = str(uuid4())
    db.start_run(key, key, {})
    costs = RunCosts(db, key)
    costs.initialize(Decimal(budget) if budget is not None else None)
    model = ModelConfig(model_id=f"test-{uuid4()}", max_tokens=100)
    saved = save_price(
        db, NewPrice(model_id=model.model_id, api="responses", region="us-east-1", pricing=price())
    )
    return costs, model, saved


def response(inputs=1000, outputs=100, **kwargs):
    return ModelResponse(
        [TextPart("ok")], usage=RequestUsage(input_tokens=inputs, output_tokens=outputs, **kwargs)
    )


def reserve(costs, model):
    return costs.reserve(model, "us-east-1", "generate", 1, "insight")


def test_budget_reservations_include_inflight_calls_and_settle_once_at_saved_prices(database):
    costs, model, saved = setup_run(database)
    call = reserve(costs, model)
    with pytest.raises(CostRefused, match="budget"):
        reserve(costs, model)
    save_price(
        database,
        NewPrice(
            model_id=model.model_id,
            api="responses",
            region="us-east-1",
            pricing=price(input_per_million="100"),
        ),
    )
    costs.settle(call, response())
    costs.settle(call, response(inputs=9999))
    result = report(database, costs.run_id, limit=10)
    assert Decimal(result["estimated_usd"]) == Decimal("0.0015")
    assert result["calls"][0]["price_id"] == saved["price_id"]
    assert result["call_count"] == result["complete_calls"] == 1


def test_resume_keeps_original_budget_spend_and_unknown_reservations(database):
    costs, model, _ = setup_run(database)
    key = reserve(costs, model)
    costs.settle(key, error="TimeoutError")
    resumed = RunCosts(database, costs.run_id)
    resumed.initialize(Decimal("100"))
    result = report(database, costs.run_id)
    assert result["budget_usd"] == "0.02"
    assert result["estimated_usd"] is None
    assert Decimal(result["committed_usd"]) == Decimal("0.0105")
    with pytest.raises(CostRefused, match="Unresolved"):
        reserve(resumed, model)


@pytest.mark.parametrize(
    "usage",
    [dict(inputs=0), dict(outputs=0), dict(cache_write_tokens=1), dict(input_audio_tokens=1)],
)
def test_incomplete_and_unsupported_usage_hold_exposure_and_block_budgeted_runs(database, usage):
    costs, model, _ = setup_run(database)
    key = reserve(costs, model)
    assert costs.settle(key, response(**usage)) == "unknown"
    with pytest.raises(CostRefused, match="Unresolved"):
        reserve(costs, model)


def test_overrun_records_observed_cost_and_stops_the_run(database):
    costs, model, _ = setup_run(database)
    key = reserve(costs, model)
    assert costs.settle(key, response(inputs=20000)) == "overrun"
    result = report(database, costs.run_id)
    assert Decimal(result["committed_usd"]) == Decimal("0.0205")
    assert result["estimated_usd"] is None
    with pytest.raises(CostRefused):
        reserve(costs, model)


def test_cached_tokens_and_pagination_keep_complete_aggregate_totals(database):
    costs, model, _ = setup_run(database, "1")
    for _ in range(3):
        costs.settle(reserve(costs, model), response(cache_read_tokens=200))
    result = report(database, costs.run_id, limit=1, offset=1)
    assert result["call_count"] == 3 and len(result["calls"]) == 1
    assert Decimal(result["estimated_usd"]) == 3 * Decimal("0.00132")


def test_unpriced_models_remain_unknown_and_cannot_start_budgeted_calls(database):
    costs, model, _ = setup_run(database)
    model.model_id = "unknown-model"
    with pytest.raises(CostRefused, match="Missing verified price"):
        reserve(costs, model)
    costs, model, _ = setup_run(database, None)
    model.model_id = "unknown-model"
    costs.settle(reserve(costs, model), response())
    result = report(database, costs.run_id)
    assert result["estimated_usd"] is None
    assert result["complete_calls"] == 0
