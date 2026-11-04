"""Ledger persistence and budget enforcement against real PostgreSQL."""

from decimal import Decimal
from uuid import uuid4

import pytest
from postgres import requires_postgres
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.usage import RequestUsage
from test_costs import price

from mel.config import ModelConfig
from mel.costs import (
    CapUpdate,
    CostRefused,
    NewPrice,
    RunCosts,
    cap_view,
    check_budget_prices,
    lookup,
    report,
    save_price,
    set_cap,
)

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


def capped_run(db):
    key = str(uuid4())
    db.start_run(key, key, {})
    costs = RunCosts(db, key)
    costs.initialize(None, capped=True)
    return costs


def test_console_runs_are_refused_until_a_spend_cap_is_set(database):
    set_cap(database, CapUpdate(expected_version=cap_view(database)["version"]))
    _, model, _ = setup_run(database, budget=None)
    with pytest.raises(CostRefused, match="spend cap"):
        reserve(capped_run(database), model)
    config = type("Config", (), {"generation": model, "review": model})()
    with pytest.raises(CostRefused, match="spend cap"):
        check_budget_prices(database, config, "us-east-1", None, capped=True)


def test_spend_cap_is_cumulative_across_console_runs_and_ignores_cli_runs(database):
    uncapped, model, _ = setup_run(database, budget=None)
    reservation = lookup(database, model, "us-east-1")[1].reservation(model.max_tokens)
    version = cap_view(database)["version"]
    set_cap(database, CapUpdate(expected_version=version, budget_usd=reservation))
    first, second = capped_run(database), capped_run(database)
    call = reserve(first, model)
    with pytest.raises(CostRefused, match="spend cap"):
        reserve(second, model)
    reserve(uncapped, model)  # Command-line runs never draw on the cap.
    first.settle(call, response(inputs=10, outputs=10))
    view = cap_view(database)
    assert Decimal(view["committed_usd"]) < reservation
    assert Decimal(view["remaining_usd"]) == reservation - Decimal(view["committed_usd"])
    with pytest.raises(CostRefused, match="spend cap"):
        reserve(second, model)  # The remaining cap is below one full reservation.


def test_unknown_console_calls_keep_their_reservation_against_the_cap(database):
    _, model, _ = setup_run(database, budget=None)
    reservation = lookup(database, model, "us-east-1")[1].reservation(model.max_tokens)
    version = cap_view(database)["version"]
    set_cap(database, CapUpdate(expected_version=version, budget_usd=reservation * 2))
    run = capped_run(database)
    run.settle(reserve(run, model), error="TimeoutError")
    assert Decimal(cap_view(database)["committed_usd"]) == reservation


def test_spend_cap_updates_need_the_current_version(database):
    version = cap_view(database)["version"]
    set_cap(database, CapUpdate(expected_version=version, budget_usd=Decimal("1")))
    with pytest.raises(CostRefused, match="changed"):
        set_cap(database, CapUpdate(expected_version=version, budget_usd=Decimal("2")))
    assert cap_view(database)["budget_usd"] == "1"


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


def test_persisted_batch_captures_generation_and_review_and_retains_results(database):
    import asyncio

    from test_jobs_postgres import active, agents, fund_console, smoke_dataset

    from mel.bundle import Bundle
    from mel.config import Settings
    from mel.datasets import InsightFilter
    from mel.jobs import BatchJobSpec, run_queue

    fund_console(database)
    config_id = active(database)
    dataset = smoke_dataset(database)
    bundle = Bundle.load(Settings(bedrock_model_id="test"))
    for model in (bundle.config.generation, bundle.config.review):
        save_price(
            database,
            NewPrice(
                model_id=model.model_id,
                api=model.api,
                region="us-east-1",
                pricing=price(max_input_tokens=1_000_000),
            ),
        )
    spec = BatchJobSpec(
        dataset_id=dataset["dataset_id"],
        config_id=config_id,
        selection=InsightFilter(limit=4),
        budget_usd="20",
    )
    job = database.create_job(str(uuid4()), "batch", spec.model_dump(mode="json"))
    asyncio.run(run_queue(database, settings=Settings(), once=True, agent_factory=agents))
    saved = database.job(job["job_id"])
    assert saved["status"] == "completed"
    result = report(database, saved["runs"][0]["run_id"], limit=200)
    assert result["complete"] and result["call_count"] > 0
    assert {r["role"] for r in result["calls"]} == {"generate", "review"}
    assert all(r["insight_id"] != "unknown" and r["attempt"] > 0 for r in result["calls"])
    assert database.results(saved["runs"][0]["run_id"])


def test_independent_connections_cannot_reserve_more_than_the_run_budget():
    import os
    from concurrent.futures import ThreadPoolExecutor

    from mel.config import Settings
    from mel.store import Store

    settings = Settings(
        database_host="localhost",
        database_port=int(os.environ["MEL_TEST_DB_PORT"]),
        database_sslmode="disable",
    )
    first, second = Store(settings), Store(settings)
    schema = "test_costs_" + uuid4().hex
    try:
        first.connection.execute(f'CREATE SCHEMA "{schema}"')
        for store in (first, second):
            store.connection.execute(f'SET search_path TO "{schema}"')
        first.initialize()
        costs, model, _ = setup_run(first)
        other = RunCosts(second, costs.run_id)

        def attempt(ledger):
            try:
                return reserve(ledger, model)
            except CostRefused:
                return None

        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(attempt, (costs, other)))
        assert sum(value is not None for value in results) == 1
        assert Decimal(report(first, costs.run_id)["committed_usd"]) == Decimal("0.0105")
    finally:
        second.close()
        first.connection.execute("SET search_path TO public")
        first.connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        first.close()
