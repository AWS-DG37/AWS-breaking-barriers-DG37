"""Versioned PostgreSQL prices, request accounting and per-run budget reservations."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import asdict
from datetime import date
from decimal import Decimal
from typing import Literal
from uuid import uuid4

from psycopg.types.json import Jsonb
from pydantic import Field, model_validator
from pydantic_ai.models.wrapper import WrapperModel

from mel.config import StrictModel

logger = logging.getLogger(__name__)


class CostRefused(Exception):
    """Stop this run before another provider request is sent."""


class Price(StrictModel):
    input_per_million: Decimal = Field(ge=0, allow_inf_nan=False, max_digits=24, decimal_places=12)
    output_per_million: Decimal = Field(ge=0, allow_inf_nan=False, max_digits=24, decimal_places=12)
    cache_read_per_million: Decimal | None = Field(
        default=None, ge=0, allow_inf_nan=False, max_digits=24, decimal_places=12
    )
    cache_write_per_million: Decimal | None = Field(
        default=None, ge=0, allow_inf_nan=False, max_digits=24, decimal_places=12
    )
    long_context_threshold: int | None = Field(default=None, gt=0)
    long_input_per_million: Decimal | None = Field(
        default=None, ge=0, allow_inf_nan=False, max_digits=24, decimal_places=12
    )
    long_output_per_million: Decimal | None = Field(
        default=None, ge=0, allow_inf_nan=False, max_digits=24, decimal_places=12
    )
    long_cache_read_per_million: Decimal | None = Field(
        default=None, ge=0, allow_inf_nan=False, max_digits=24, decimal_places=12
    )
    long_cache_write_per_million: Decimal | None = Field(
        default=None, ge=0, allow_inf_nan=False, max_digits=24, decimal_places=12
    )
    max_input_tokens: int = Field(gt=0, le=10_000_000)
    source: str = Field(min_length=1, max_length=2000)
    checked_on: date
    notes: str = Field(default="", max_length=4000)

    @model_validator(mode="after")
    def tiers(self):
        if self.long_context_threshold is not None and (
            self.long_input_per_million is None or self.long_output_per_million is None
        ):
            raise ValueError("Long-context pricing needs both input and output rates")
        return self

    def estimate(self, inputs: int, outputs: int, cached: int = 0, writes: int = 0) -> Decimal:
        long = self.long_context_threshold is not None and inputs > self.long_context_threshold
        in_rate = self.long_input_per_million if long else self.input_per_million
        out_rate = self.long_output_per_million if long else self.output_per_million
        cache_rate = self.long_cache_read_per_million if long else self.cache_read_per_million
        write_rate = self.long_cache_write_per_million if long else self.cache_write_per_million
        if inputs < cached + writes or min(inputs, outputs, cached, writes) < 0:
            raise ValueError("Invalid token counts")
        if cached and cache_rate is None:
            raise ValueError("Cache reads have no verified price")
        if writes and write_rate is None:
            raise ValueError("Cache writes have no verified price")
        return (
            (inputs - cached - writes) * in_rate
            + outputs * out_rate
            + cached * (cache_rate or 0)
            + writes * (write_rate or 0)
        ) / 1_000_000

    def reservation(self, outputs: int) -> Decimal:
        # Full model context, using the most expensive configured input dimension.
        input_rate = max(
            self.input_per_million,
            self.cache_read_per_million or 0,
            self.cache_write_per_million or 0,
            self.long_input_per_million or 0,
            self.long_cache_read_per_million or 0,
            self.long_cache_write_per_million or 0,
        )
        output_rate = max(self.output_per_million, self.long_output_per_million or 0)
        return (self.max_input_tokens * input_rate + outputs * output_rate) / 1_000_000


class CapUpdate(StrictModel):
    expected_version: int = Field(ge=0)
    budget_usd: Decimal | None = Field(
        default=None, gt=0, allow_inf_nan=False, max_digits=24, decimal_places=12
    )


CAP_REFUSED = "Set a console spend cap before running models"
# Unsettled and unknown calls hold their full reservation, so the cap never under-counts.
CAP_EXPOSURE = (
    "SELECT coalesce(sum(CASE WHEN c.state='complete' THEN c.estimated_usd "
    "ELSE greatest(c.reserved_usd,coalesce(c.estimated_usd,0)) END),0) "
    "FROM model_calls c JOIN run_costs r USING(run_id) WHERE r.capped"
)


def cap_view(store):
    with store.lock, store.connection.transaction():
        version, budget = store.connection.execute(
            "SELECT version,budget_usd FROM spend_cap"
        ).fetchone()
        committed = store.connection.execute(CAP_EXPOSURE).fetchone()[0]
    return dict(
        version=version,
        budget_usd=str(budget) if budget is not None else None,
        committed_usd=str(committed),
        remaining_usd=str(max(Decimal(0), budget - committed)) if budget is not None else None,
        scope="All runs the console queues. Command-line runs are outside this cap.",
    )


def set_cap(store, update: CapUpdate):
    with store.lock, store.connection.transaction():
        version = store.connection.execute("SELECT version FROM spend_cap FOR UPDATE").fetchone()[0]
        if version != update.expected_version:
            raise CostRefused("The spend cap changed. Refresh before saving.")
        store.connection.execute(
            "UPDATE spend_cap SET version=version+1,budget_usd=%s", (update.budget_usd,)
        )
    return cap_view(store)


def _cap_remaining(connection):
    budget = connection.execute("SELECT budget_usd FROM spend_cap FOR UPDATE").fetchone()[0]
    if budget is None:
        raise CostRefused(CAP_REFUSED)
    return budget - connection.execute(CAP_EXPOSURE).fetchone()[0]


class NewPrice(StrictModel):
    model_id: str = Field(min_length=1, max_length=300)
    api: Literal["responses", "converse"]
    region: str = Field(min_length=1, max_length=100)
    pricing: Price


# Each coroutine has its own insight/graph attempt, including concurrent batch workers.
identity: ContextVar[tuple[str, int] | None] = ContextVar("cost_identity", default=None)


@contextmanager
def request_scope(insight_id: str, attempt: int = 1):
    token = identity.set((insight_id, attempt))
    try:
        yield
    finally:
        identity.reset(token)


def save_price(store, value: NewPrice):
    key = str(uuid4())
    with store.lock:
        store.connection.execute(
            "INSERT INTO model_prices(price_id,model_id,api,region,pricing) "
            "VALUES (%s,%s,%s,%s,%s)",
            (
                key,
                value.model_id,
                value.api,
                value.region,
                Jsonb(value.pricing.model_dump(mode="json")),
            ),
        )
    return {"price_id": key, **value.model_dump(mode="json")}


def prices(store):
    with store.lock:
        rows = store.connection.execute(
            "SELECT DISTINCT ON (model_id,api,region) price_id,model_id,api,region,pricing "
            "FROM model_prices ORDER BY model_id,api,region,version DESC"
        ).fetchall()
    return [
        dict(zip(("price_id", "model_id", "api", "region", "pricing"), (str(r[0]), *r[1:])))
        for r in rows
    ]


def lookup(store, model, region):
    row = store.connection.execute(
        "SELECT price_id,pricing FROM model_prices WHERE model_id=%s AND api=%s AND region=%s "
        "ORDER BY version DESC LIMIT 1",
        (model.model_id, model.api, region),
    ).fetchone()
    return (str(row[0]), Price.model_validate(row[1])) if row else (None, None)


class RunCosts:
    def __init__(self, store, run_id):
        self.store, self.run_id = store, run_id

    def initialize(self, budget, capped=False):
        with self.store.lock:
            # A resumed run keeps its original limit, cap membership and prior exposure.
            self.store.connection.execute(
                "INSERT INTO run_costs(run_id,budget_usd,capped) VALUES (%s,%s,%s) "
                "ON CONFLICT DO NOTHING",
                (self.run_id, budget, capped),
            )

    def reserve(self, model, region, role, attempt, insight_id):
        with self.store.lock, self.store.connection.transaction():
            capped = self.store.connection.execute(
                "SELECT capped FROM run_costs WHERE run_id=%s", (self.run_id,)
            ).fetchone()[0]
            # Lock order is spend_cap, then run_costs, so concurrent workers cannot deadlock.
            cap_remaining = _cap_remaining(self.store.connection) if capped else None
            budget, stopped = self.store.connection.execute(
                "SELECT budget_usd,stop_reason FROM run_costs WHERE run_id=%s FOR UPDATE",
                (self.run_id,),
            ).fetchone()
            if stopped:
                raise CostRefused(stopped)
            price_id, price = lookup(self.store, model, region)
            amount = price.reservation(model.max_tokens) if price else Decimal(0)
            if capped:
                if price is None:
                    raise CostRefused(
                        f"Missing verified price: {model.model_id} / {model.api} / {region}"
                    )
                if amount > cap_remaining:
                    raise CostRefused("Console spend cap cannot cover the next request reservation")
            if budget is not None:
                exposure, unresolved = self.store.connection.execute(
                    "SELECT coalesce(sum(CASE WHEN state='complete' THEN estimated_usd "
                    "ELSE greatest(reserved_usd,coalesce(estimated_usd,0)) END),0), "
                    "count(*) FILTER (WHERE state IN ('unknown','overrun','unpriced')) "
                    "FROM model_calls WHERE run_id=%s",
                    (self.run_id,),
                ).fetchone()
                if price is None:
                    raise CostRefused(
                        f"Missing verified price: {model.model_id} / {model.api} / {region}"
                    )
                if unresolved:
                    raise CostRefused(
                        "Unresolved request costs; this run cannot send more requests"
                    )
                if exposure + amount > budget:
                    raise CostRefused("Run budget cannot cover the next request reservation")
            key = str(uuid4())
            self.store.connection.execute(
                "INSERT INTO model_calls(call_id,run_id,insight_id,role,attempt,"
                "model_id,api,region,"
                "price_id,reserved_usd,max_output_tokens) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    key,
                    self.run_id,
                    insight_id,
                    role,
                    attempt,
                    model.model_id,
                    model.api,
                    region,
                    price_id,
                    amount,
                    model.max_tokens,
                ),
            )
            return key

    def stop(self, reason):
        with self.store.lock:
            self.store.connection.execute(
                "UPDATE run_costs SET stop_reason=coalesce(stop_reason,%s) WHERE run_id=%s",
                (reason, self.run_id),
            )

    def settle(self, key, response=None, error=None):
        with self.store.lock, self.store.connection.transaction():
            budget = self.store.connection.execute(
                "SELECT budget_usd FROM run_costs WHERE run_id=%s FOR UPDATE",
                (self.run_id,),
            ).fetchone()[0]
            row = self.store.connection.execute(
                "SELECT c.state,c.reserved_usd,c.max_output_tokens,p.pricing FROM model_calls c "
                "LEFT JOIN model_prices p USING(price_id) WHERE call_id=%s",
                (key,),
            ).fetchone()
            if row[0] != "pending":
                return row[0]
            _, reserved, max_output, data = row
            usage = response.usage if response else None
            inputs = usage.input_tokens if usage and usage.input_tokens > 0 else None
            outputs = usage.output_tokens if usage and usage.output_tokens > 0 else None
            state, estimate = "unknown", None
            if usage and inputs is not None and outputs is not None and error is None:
                unsupported = any(
                    (
                        usage.input_audio_tokens,
                        usage.output_audio_tokens,
                        usage.cache_audio_read_tokens,
                    )
                )
                if not unsupported:
                    if data is None:
                        state = "unpriced"
                    else:
                        price = Price.model_validate(data)
                        try:
                            estimate = price.estimate(
                                inputs, outputs, usage.cache_read_tokens, usage.cache_write_tokens
                            )
                            state = "complete"
                            if (
                                inputs > price.max_input_tokens
                                or outputs > max_output
                                or estimate > reserved
                            ):
                                state = "overrun"
                        except ValueError:
                            error = "UnsupportedUsage"
                else:
                    error = "UnsupportedUsage"
            self.store.connection.execute(
                "UPDATE model_calls SET state=%s,input_tokens=%s,output_tokens=%s,estimated_usd=%s,"
                "usage=%s,provider_request_id=%s,error_type=%s,settled_at=now() WHERE call_id=%s",
                (
                    state,
                    inputs,
                    outputs,
                    estimate,
                    Jsonb(asdict(usage)) if usage else None,
                    response.provider_response_id if response else None,
                    error,
                    key,
                ),
            )
            if budget is not None and state != "complete":
                self.stop("Unresolved request costs; this run cannot send more requests")
            return state


def report(store, run_id, *, limit=0, offset=0):
    with store.lock, store.connection.transaction():
        row = store.connection.execute(
            "SELECT budget_usd,stop_reason FROM run_costs WHERE run_id=%s",
            (run_id,),
        ).fetchone()
        if row is None:
            return {"available": False, "estimated_usd": None}
        count, complete, known, exposure, inputs, outputs = store.connection.execute(
            "SELECT count(*), count(*) FILTER (WHERE state='complete'), "
            "coalesce(sum(estimated_usd),0), "
            "coalesce(sum(CASE WHEN state='complete' THEN estimated_usd "
            "ELSE greatest(reserved_usd,coalesce(estimated_usd,0)) END),0), "
            "coalesce(sum(input_tokens),0),coalesce(sum(output_tokens),0) "
            "FROM model_calls WHERE run_id=%s",
            (run_id,),
        ).fetchone()
        calls = []
        if limit:
            rows = store.connection.execute(
                "SELECT c.call_id,c.insight_id,c.role,c.attempt,c.model_id,c.api,c.region,c.state,"
                "c.input_tokens,c.output_tokens,c.estimated_usd,c.reserved_usd,c.price_id,"
                "c.provider_request_id,c.error_type,p.pricing FROM model_calls c "
                "LEFT JOIN model_prices p USING(price_id) WHERE run_id=%s "
                "ORDER BY c.created_at,c.call_id LIMIT %s OFFSET %s",
                (run_id, limit, offset),
            ).fetchall()
            columns = (
                "call_id",
                "insight_id",
                "role",
                "attempt",
                "model_id",
                "api",
                "region",
                "state",
                "input_tokens",
                "output_tokens",
                "estimated_usd",
                "reserved_usd",
                "price_id",
                "provider_request_id",
                "error_type",
                "pricing",
            )
            calls = [
                {
                    k: str(v) if k in ("call_id", "price_id") and v is not None else v
                    for k, v in zip(columns, r)
                }
                for r in rows
            ]
    return dict(
        available=True,
        budget_usd=str(row[0]) if row[0] is not None else None,
        stop_reason=row[1],
        estimated_usd=str(known) if complete == count else None,
        known_estimated_usd=str(known),
        committed_usd=str(exposure),
        remaining_usd=str(max(Decimal(0), row[0] - exposure)) if row[0] is not None else None,
        complete=complete == count,
        call_count=count,
        complete_calls=complete,
        known_input_tokens=inputs,
        known_output_tokens=outputs,
        calls=calls,
        offset=offset,
        limit=limit,
    )


class CostedModel(WrapperModel):
    def __init__(self, wrapped, costs, config, region, role):
        super().__init__(wrapped)
        self.costs, self.config, self.region, self.role = costs, config, region, role

    async def settle(self, key, response=None, error=None):
        try:
            return await asyncio.shield(asyncio.to_thread(self.costs.settle, key, response, error))
        except asyncio.CancelledError:
            raise
        except Exception as failure:
            logger.exception("Could not settle request accounting for call %s", key)
            try:
                await asyncio.to_thread(self.costs.stop, "Request accounting failed")
            except Exception:
                pass  # The pending reservation remains held if storage is unavailable.
            raise CostRefused("Request accounting failed") from failure

    async def request(self, messages, model_settings, model_request_parameters):
        insight_id, attempt = identity.get() or ("unknown", 1)
        try:
            key = await asyncio.to_thread(
                self.costs.reserve,
                self.config,
                self.region,
                self.role,
                attempt,
                insight_id,
            )
        except CostRefused as error:
            await asyncio.to_thread(self.costs.stop, str(error))
            raise
        try:
            response = await self.wrapped.request(
                messages,
                {**(model_settings or {}), "max_tokens": self.config.max_tokens},
                model_request_parameters,
            )
        except BaseException as error:
            await self.settle(key, error=type(error).__name__)
            raise
        state = await self.settle(key, response)
        if state in ("unknown", "overrun"):
            # Unbudgeted runs still retain unknown records; budgeted runs stop here.
            with self.costs.store.lock:
                stopped = self.costs.store.connection.execute(
                    "SELECT stop_reason FROM run_costs WHERE run_id=%s",
                    (self.costs.run_id,),
                ).fetchone()[0]
            if stopped:
                raise CostRefused(stopped)
        return response

    @asynccontextmanager
    async def request_stream(self, *args, **kwargs):
        raise CostRefused("Streaming is not supported by run accounting")
        yield


def meter_agents(store, run_id, config, region, agents, budget=None, capped=False):
    costs = RunCosts(store, run_id)
    costs.initialize(budget, capped)
    for agent, model, role in zip(
        agents, (config.generation, config.review), ("generate", "review")
    ):
        agent.model = CostedModel(agent.model, costs, model, region, role)
    return agents


def estimate_run(store, config, region, surveys):
    """Rough ordinary-call estimate plus conservative per-request reservations."""
    entries, total = [], Decimal(0)
    with store.lock:
        for role, model in (("generate", config.generation), ("review", config.review)):
            price_id, price = lookup(store, model, region)
            rough = (
                price.estimate(3300, 200) * surveys
                if price
                else (Decimal(0) if surveys == 0 else None)
            )
            if rough is not None:
                total += rough
            entries.append(
                dict(
                    role=role,
                    model_id=model.model_id,
                    api=model.api,
                    price_id=price_id,
                    estimated_usd=str(rough) if rough is not None else None,
                    request_reservation_usd=str(price.reservation(model.max_tokens))
                    if price
                    else None,
                    maximum_requests=surveys
                    * (config.graph.max_revisions + 1)
                    * config.graph.max_requests_per_call,
                )
            )
    return dict(
        estimated_usd=str(total) if surveys == 0 or all(e["price_id"] for e in entries) else None,
        models=entries,
        note="Rough estimate for one generation and one review per survey; "
        "revisions and output retries add cost. Reservations use full model context bounds.",
    )


def check_budget_prices(store, config, region, budget, capped=False):
    if budget is None and not capped:
        return
    remaining = None
    if capped:
        cap = cap_view(store)
        if cap["remaining_usd"] is None:
            raise CostRefused(CAP_REFUSED)
        remaining = Decimal(cap["remaining_usd"])
    with store.lock:
        for model in (config.generation, config.review):
            _, price = lookup(store, model, region)
            if price is None:
                raise CostRefused(
                    f"Missing verified price: {model.model_id} / {model.api} / {region}"
                )
            reservation = price.reservation(model.max_tokens)
            if budget is not None and reservation > budget:
                raise CostRefused("Run budget is below a selected model's request reservation")
            if remaining is not None and reservation > remaining:
                raise CostRefused(
                    "Console spend cap cannot cover a selected model's request reservation"
                )
