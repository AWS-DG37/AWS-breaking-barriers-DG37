"""Website admission and estimated cost accounting. Batch workers are outside this budget."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256
from typing import Literal
from uuid import uuid4

from pydantic import Field, model_validator
from pydantic_ai.models.wrapper import WrapperModel

from mel.config import ModelConfig, StrictModel


class UsageRefused(Exception):
    pass


class PolicyConflict(UsageRefused):
    pass


class Price(StrictModel):
    model_id: str = Field(min_length=1, max_length=300, pattern=r"\S")
    api: Literal["responses", "converse"]
    region: str = Field(min_length=1, max_length=100, pattern=r"\S")
    input_per_million: Decimal = Field(ge=0, allow_inf_nan=False, max_digits=24, decimal_places=12)
    output_per_million: Decimal = Field(ge=0, allow_inf_nan=False, max_digits=24, decimal_places=12)
    max_input_tokens: int = Field(gt=0, le=2_000_000_000)
    source: str = Field(min_length=1, max_length=2000, pattern=r"\S")
    bound_source: str = Field(min_length=1, max_length=2000, pattern=r"\S")

    def estimate(self, inputs: int, outputs: int) -> Decimal:
        return (inputs * self.input_per_million + outputs * self.output_per_million) / 1_000_000


class PolicyUpdate(StrictModel):
    expected_version: int = Field(ge=0)
    enabled: bool
    budget_usd: Decimal | None = Field(
        default=None, gt=0, allow_inf_nan=False, max_digits=24, decimal_places=12
    )
    prices: list[Price] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def configured(self):
        keys = [(p.model_id, p.api, p.region) for p in self.prices]
        if len(keys) != len(set(keys)):
            raise ValueError("Each model, API and region needs one price")
        if self.enabled and (self.budget_usd is None or not self.prices):
            raise ValueError("Enabling requires a positive budget and sourced prices")
        return self


@dataclass(frozen=True)
class CallIdentity:
    run_id: str
    insight_id: str
    role: Literal["generate", "review"]
    attempt: int


@dataclass(frozen=True)
class Reservation:
    call_id: str
    reserved_usd: Decimal


gateway_context: ContextVar[UsageGateway | None] = ContextVar("website_gateway", default=None)
identity_context: ContextVar[CallIdentity | None] = ContextVar("website_call", default=None)


@contextmanager
def usage_scope(*, gateway=None, identity=None):
    tokens = []
    if gateway is not None:
        tokens.append((gateway_context, gateway_context.set(gateway)))
    if identity is not None:
        tokens.append((identity_context, identity_context.set(identity)))
    try:
        yield
    finally:
        for variable, token in reversed(tokens):
            variable.reset(token)


class UsageGateway:
    def __init__(self, store, *, host_allowed: bool, expected_version: int | None = None):
        self.store = store
        self.host_allowed = host_allowed
        self.expected_version = expected_version

    @contextmanager
    def transaction(self):
        with self.store.lock, self.store.connection.transaction():
            row = self.store.connection.execute(
                "SELECT version,enabled,budget_usd FROM website_policy WHERE singleton FOR UPDATE"
            ).fetchone()
            yield row

    def _totals(self):
        return self.store.connection.execute(
            "SELECT COALESCE(sum(estimated_usd),0), "
            "COALESCE(sum(CASE WHEN state IN ('pending','unknown') THEN "
            "reserved_usd ELSE 0 END),0), "
            "count(*) FILTER (WHERE state IN ('unknown','overrun')), "
            "count(*) FILTER (WHERE state='pending'), "
            "COALESCE(sum(CASE WHEN state IN ('pending','unknown') THEN "
            "greatest(reserved_usd,COALESCE(estimated_usd,0)) ELSE "
            "COALESCE(estimated_usd,0) END),0) FROM website_calls"
        ).fetchone()

    def _view(self, policy):
        version, enabled, budget = policy
        estimated, held, unresolved, pending, committed = self._totals()
        rows = self.store.connection.execute(
            "SELECT model_id,api,region,input_per_million,output_per_million,max_input_tokens,"
            "source,bound_source FROM website_prices WHERE policy_version=%s ORDER BY model_id,api",
            (version,),
        ).fetchall()
        prices = [
            Price(**dict(zip(Price.model_fields, row))).model_dump(mode="json") for row in rows
        ]
        blockers = []
        if not self.host_allowed:
            blockers.append("Host has disabled live execution")
        if not enabled:
            blockers.append("Live execution is disabled")
        if budget is None:
            blockers.append("Set a positive cumulative budget")
        if not prices:
            blockers.append("Add sourced model prices and input bounds")
        if unresolved:
            blockers.append("An unresolved request needs operator reconciliation")
        remaining = budget - committed if budget is not None else None
        if remaining is not None and remaining <= 0:
            blockers.append("Budget is fully committed")
        return dict(
            version=version,
            enabled=enabled,
            host_allowed=self.host_allowed,
            effective=not blockers,
            blockers=blockers,
            budget_usd=str(budget) if budget is not None else None,
            estimated_usd=str(estimated),
            held_usd=str(held),
            committed_usd=str(committed),
            remaining_usd=str(remaining) if remaining is not None else None,
            unresolved_calls=unresolved,
            pending_calls=pending,
            prices=prices,
            scope="Website requests only. CLI and batch work are outside this budget.",
        )

    def view(self):
        with self.transaction() as policy:
            return self._view(policy)

    def configure(self, update: PolicyUpdate):
        with self.transaction() as policy:
            if policy[0] != update.expected_version:
                raise PolicyConflict("Controls changed. Refresh before saving.")
            version = policy[0] + 1
            self.store.connection.execute(
                "UPDATE website_policy SET version=%s,enabled=%s,budget_usd=%s WHERE singleton",
                (version, update.enabled, update.budget_usd),
            )
            for price in update.prices:
                self.store.connection.execute(
                    "INSERT INTO website_prices(price_id,policy_version,model_id,api,region,"
                    "input_per_million,output_per_million,max_input_tokens,source,bound_source) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (str(uuid4()), version, *price.model_dump().values()),
                )
            return self._view((version, update.enabled, update.budget_usd))

    def _admit(self, policy, expected_version=None):
        if expected_version is None:
            expected_version = self.expected_version
        if expected_version is not None and policy[0] != expected_version:
            raise PolicyConflict("Controls changed. Review a fresh estimate before launching.")
        view = self._view(policy)
        if not view["effective"]:
            raise UsageRefused("; ".join(view["blockers"]))
        return Decimal(view["remaining_usd"])

    def _price(self, version, model, region):
        row = self.store.connection.execute(
            "SELECT price_id,model_id,api,region,input_per_million,output_per_million,"
            "max_input_tokens,source,bound_source FROM website_prices "
            "WHERE policy_version=%s AND model_id=%s AND api=%s AND region=%s",
            (version, model.model_id, model.api, region),
        ).fetchone()
        if row is None:
            raise UsageRefused(
                f"No sourced price and input bound for {model.model_id} / {model.api} / {region}"
            )
        return str(row[0]), Price(**dict(zip(Price.model_fields, row[1:])))

    def reserve(self, model: ModelConfig, region: str, identity: CallIdentity):
        with self.transaction() as policy:
            remaining = self._admit(policy)
            price_id, price = self._price(policy[0], model, region)
            amount = price.estimate(price.max_input_tokens, model.max_tokens)
            if amount > remaining:
                raise UsageRefused("Remaining budget cannot cover the next request reservation")
            call_id = str(uuid4())
            self.store.connection.execute(
                "INSERT INTO website_calls(call_id,price_id,run_id,insight_id,role,attempt,"
                "max_output_tokens,reserved_usd) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    call_id,
                    price_id,
                    identity.run_id,
                    identity.insight_id,
                    identity.role,
                    identity.attempt,
                    model.max_tokens,
                    amount,
                ),
            )
            return Reservation(call_id, amount)

    def settle(self, call_id, inputs, outputs, *, error_type=None, provider_request_id=None):
        with self.transaction():
            row = self.store.connection.execute(
                "SELECT c.state,c.reserved_usd,p.input_per_million,p.output_per_million,"
                "p.max_input_tokens,c.max_output_tokens FROM website_calls c "
                "JOIN website_prices p USING(price_id) WHERE c.call_id=%s",
                (call_id,),
            ).fetchone()
            if row is None or row[0] != "pending":
                return
            _, reserved, input_rate, output_rate, max_input, max_output = row
            known = [value for value in (inputs, outputs) if value is not None]
            if any(not isinstance(value, int) or value < 0 for value in known):
                inputs = outputs = None
            complete = inputs is not None and outputs is not None and error_type is None
            estimate = ((inputs or 0) * input_rate + (outputs or 0) * output_rate) / 1_000_000
            state = "settled" if complete else "unknown"
            if estimate > reserved or (inputs or 0) > max_input or (outputs or 0) > max_output:
                state = "overrun" if complete else "unknown"
            self.store.connection.execute(
                "UPDATE website_calls SET state=%s,input_tokens=%s,output_tokens=%s,"
                "estimated_usd=%s,error_type=%s,provider_request_id=%s,settled_at=now() "
                "WHERE call_id=%s",
                (
                    state,
                    inputs,
                    outputs,
                    estimate if known else None,
                    error_type,
                    provider_request_id,
                    call_id,
                ),
            )
            return state

    def quote(self, spec, region, graph, *, expected_version=None):
        with self.transaction() as policy:
            view = self._view(policy)
            blockers = list(view["blockers"])
            if expected_version is not None and policy[0] != expected_version:
                raise PolicyConflict("Controls changed. Review a fresh estimate before launching.")
            entries = []
            for candidate in spec.candidates:
                for role, model in [("generate", candidate.model), ("review", spec.reviewer)]:
                    try:
                        _, price = self._price(policy[0], model, region)
                    except UsageRefused as error:
                        blockers.append(str(error))
                        continue
                    reservation = price.estimate(price.max_input_tokens, model.max_tokens)
                    calls = min(graph.max_requests_per_call, graph.agent_output_retries + 1)
                    calls *= (graph.max_revisions + 1) * min(spec.row_limit, len(spec.insights))
                    entries.append(
                        dict(
                            candidate=candidate.label,
                            role=role,
                            model_id=model.model_id,
                            api=model.api,
                            region=region,
                            request_reservation_usd=str(reservation),
                            maximum_requests=calls,
                            maximum_estimated_usd=str(reservation * calls),
                        )
                    )
            maximum = sum((Decimal(e["maximum_estimated_usd"]) for e in entries), Decimal(0))
            remaining = Decimal(view["remaining_usd"]) if view["remaining_usd"] else Decimal(0)
            if any(Decimal(e["request_reservation_usd"]) > remaining for e in entries):
                blockers.append("Remaining budget cannot cover a selected request reservation")
            terms = dict(
                spec=spec.model_dump(mode="json"),
                graph=graph.model_dump(mode="json"),
                region=region,
                policy_version=policy[0],
            )
            terms_hash = sha256(json.dumps(terms, sort_keys=True).encode()).hexdigest()
            return dict(
                terms_hash=terms_hash,
                policy_version=policy[0],
                effective=not blockers,
                blockers=list(dict.fromkeys(blockers)),
                requests=entries,
                maximum_estimated_usd=str(maximum),
                remaining_usd=view["remaining_usd"],
                note="Pessimistic configured-price estimate. Each request reserves its "
                "full documented input bound and output limit. Execution stops when "
                "remaining budget cannot cover the next request. This is not an "
                "AWS invoice or an account-wide cap.",
            )

    def launch(self, launch_id, spec, version, region, graph, *, quote_hash):
        import json

        from psycopg.types.json import Jsonb

        payload = spec.model_dump(mode="json")
        digest = sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        with self.transaction():
            previous = self.store.connection.execute(
                "SELECT spec_hash,policy_version,experiment_id FROM website_launches "
                "WHERE launch_id=%s",
                (launch_id,),
            ).fetchone()
            if previous:
                if previous[:2] != (digest, version):
                    raise PolicyConflict("Launch identity was already used with different terms")
                return str(previous[2]), False
            quote = self.quote(spec, region, graph, expected_version=version)
            if quote["terms_hash"] != quote_hash:
                raise PolicyConflict("Request configuration changed. Review a fresh estimate.")
            if not quote["effective"]:
                raise UsageRefused("; ".join(quote["blockers"]))
            key = str(uuid4())
            self.store.connection.execute(
                "INSERT INTO experiments(experiment_id,spec,source) VALUES (%s,%s,'live')",
                (key, Jsonb(payload)),
            )
            self.store.connection.execute(
                "INSERT INTO website_launches(launch_id,spec_hash,policy_version,experiment_id) "
                "VALUES (%s,%s,%s,%s)",
                (launch_id, digest, version, key),
            )
            return key, True

    def analysis(self, *, run_id=None, experiment_id=None):
        params = (run_id, run_id, experiment_id, experiment_id)
        with self.store.lock, self.store.connection.transaction():
            rows = self.store.connection.execute(
                "SELECT c.call_id,c.run_id,c.insight_id,c.role,c.attempt,c.state,c.reserved_usd,"
                "c.input_tokens,c.output_tokens,c.estimated_usd,p.model_id,p.api,p.region "
                "FROM website_calls c JOIN website_prices p USING(price_id) "
                "LEFT JOIN generation_runs r ON r.run_id=c.run_id "
                "WHERE (%s::uuid IS NULL OR c.run_id=%s::uuid) "
                "AND (%s::uuid IS NULL OR r.experiment_id=%s::uuid) "
                "ORDER BY c.created_at,c.call_id",
                params,
            ).fetchall()
            historical = self.store.connection.execute(
                "SELECT CASE WHEN e.source='recorded' THEN 'recorded' ELSE 'other' END, "
                "i.outcome FROM insight_results i JOIN generation_runs r USING(run_id) "
                "LEFT JOIN experiments e USING(experiment_id) "
                "WHERE (%s::uuid IS NULL OR r.run_id=%s::uuid) "
                "AND (%s::uuid IS NULL OR r.experiment_id=%s::uuid) "
                "AND NOT EXISTS (SELECT 1 FROM website_calls c WHERE c.run_id=i.run_id "
                "AND c.insight_id=i.insight_id)",
                params,
            ).fetchall()
        groups = {}
        calls = []
        raw_calls = []
        for row in rows:
            call = dict(
                zip(
                    (
                        "call_id",
                        "run_id",
                        "insight_id",
                        "role",
                        "attempt",
                        "state",
                        "reserved_usd",
                        "input_tokens",
                        "output_tokens",
                        "estimated_usd",
                        "model_id",
                        "api",
                        "region",
                    ),
                    row,
                )
            )
            raw_calls.append(call)
            calls.append(
                {
                    k: str(v) if isinstance(v, Decimal) or k in ("call_id", "run_id") else v
                    for k, v in call.items()
                }
            )
            key = (call["model_id"], call["api"], call["region"], call["role"])
            groups.setdefault(key, []).append(call)

        def total(values):
            complete = sum(v["state"] == "settled" for v in values)
            subtotal = sum((v["estimated_usd"] or Decimal(0) for v in values), Decimal(0))
            has_estimate = any(v["estimated_usd"] is not None for v in values)
            return dict(
                calls=len(values),
                complete_calls=complete,
                input_tokens=sum(v["input_tokens"] or 0 for v in values),
                output_tokens=sum(v["output_tokens"] or 0 for v in values),
                usage_complete=complete == len(values),
                estimated_usd=str(subtotal) if values and complete == len(values) else None,
                known_estimated_usd=str(subtotal) if has_estimate else None,
            )

        history = {}
        for source in ("recorded", "other"):
            values = [v for kind, v in historical if kind == source]
            history[source] = dict(
                outcomes=len(values),
                complete_outcomes=sum(v.get("usage_complete") is True for v in values),
                input_tokens=sum(v.get("input_tokens") or 0 for v in values),
                output_tokens=sum(v.get("output_tokens") or 0 for v in values),
                estimated_usd=None,
                note="Known saved subtotals. Missing usage and prices are unknown. "
                "Historical role attribution is not available.",
            )
        return dict(
            metered=total(raw_calls),
            historical=history,
            breakdown=[
                dict(model_id=k[0], api=k[1], region=k[2], role=k[3], stage=k[3], **total(v))
                for k, v in groups.items()
            ],
            calls=calls if run_id or experiment_id else None,
            note="Observed token subtotals and configured-price estimates. Missing counts "
            "are unknown. Recorded and other work do not consume the website budget.",
        )


class MeteredModel(WrapperModel):
    def __init__(self, wrapped, gateway, config, region, role):
        super().__init__(wrapped)
        self.gateway, self.config, self.region, self.role = gateway, config, region, role

    async def request(self, messages, model_settings, model_request_parameters):
        identity = identity_context.get()
        if identity is None or identity.role != self.role:
            raise UsageRefused("Website request identity is missing")
        settings = {**(model_settings or {}), "max_tokens": self.config.max_tokens}
        reservation = await asyncio.to_thread(
            self.gateway.reserve, self.config, self.region, identity
        )
        try:
            response = await self.wrapped.request(messages, settings, model_request_parameters)
        except BaseException as error:
            await asyncio.shield(
                asyncio.to_thread(
                    self.gateway.settle,
                    reservation.call_id,
                    None,
                    None,
                    error_type=type(error).__name__,
                )
            )
            if isinstance(error, Exception):
                raise UsageRefused(
                    "Request failed. Its reservation remains held for reconciliation."
                ) from error
            raise
        usage = response.usage
        # SDK defaults erase absent dimensions. Positive text counts establish both were reported;
        # zero or cache/audio billing remains unresolved rather than pretending to be free.
        complete = (
            usage.input_tokens > 0
            and usage.output_tokens > 0
            and not any(
                (
                    usage.cache_read_tokens,
                    usage.cache_write_tokens,
                    usage.input_audio_tokens,
                    usage.output_audio_tokens,
                    usage.cache_audio_read_tokens,
                )
            )
        )
        state = await asyncio.shield(
            asyncio.to_thread(
                self.gateway.settle,
                reservation.call_id,
                usage.input_tokens if usage.input_tokens > 0 else None,
                usage.output_tokens if usage.output_tokens > 0 else None,
                error_type=None if complete else "IncompleteOrUnsupportedUsage",
                provider_request_id=response.provider_response_id,
            )
        )
        if not complete or state != "settled":
            raise UsageRefused("Request accounting is unresolved. Further execution is blocked.")
        return response

    @asynccontextmanager
    async def request_stream(self, *args, **kwargs):
        raise UsageRefused("Streaming is not supported by website accounting")
        yield
