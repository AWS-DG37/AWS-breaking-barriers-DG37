"""Data contracts for the Digital Green MEL survey agent.

Hand-written Pydantic models mirror the JSON Schemas in data/pack/json/schemas/
(participant pack v1). The schemas are the source of truth: `validate()` checks
any payload against them with jsonschema, and the tests run both.
"""
from __future__ import annotations

import json
from datetime import datetime
from functools import cache
from pathlib import Path
from typing import Literal, Union

from jsonschema import Draft202012Validator, FormatChecker
from pydantic import BaseModel, ConfigDict, Field
from referencing import Registry, Resource

PACK = Path(__file__).resolve().parent.parent / "docs" / "participant-pack"
SCHEMAS = PACK / "json" / "schemas"
SCHEMA_NAMES = ("insight", "survey", "schedule_handoff", "response", "no_survey", "bottom_sheet")

Channel = Literal["app", "web", "whatsapp", "telegram", "voice"]
QuestionType = Literal["survey-single-select", "survey-confirm", "survey-scale"]
ResolutionType = Literal[
    "direct_action", "generic_information", "plan_created", "needs_more_context",
    "do_not_act", "wait_and_monitor", "escalate_to_expert",
]
FollowUpReason = Literal[
    "advice_did_not_work", "crop_or_animal_damaged", "could_not_get_input", "could_not_reach_expert",
]
Timing = Literal["same_day", "next_day", "two_to_five_days", "three_to_ten_days",
                 "seven_to_fourteen_days", "seasonal", "none"]
Method = Literal["push", "whatsapp", "ivr_callback", "repeat_photo", "agent_followup", "none"]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------- insight ----------
class Location(Strict):
    country: str = Field(pattern=r"^[A-Z]{2}$")
    state: str
    district: str


class Asset(Strict):
    name: str | None
    category: Literal["crop", "livestock"] | None
    growth_stage: str | None


class Verification(Strict):
    needed: bool
    timing: Timing
    method: Method


class Insight(Strict):
    insight_id: str
    farmer_id: str
    conversation_id: str
    created_at: datetime
    conversation_language: str = Field(pattern=r"^[a-z]{2,3}-[A-Z]{2}$")
    location: Location
    asset: Asset
    concern: str | None
    behavioral_intent: str
    decision_episode_type: str | None
    severity: Literal["low", "medium", "high", "critical"] | None
    query_summary: str
    advice_summary: str
    resolution_type: ResolutionType
    action_window_days: int | None = Field(ge=0)
    verification: Verification
    preferred_channel: Channel
    quality_flag: Literal["clean", "junk"] | None = None  # practice data only


# ---------- survey ----------
class Option(Strict):
    value: str = Field(pattern=r"^[a-z0-9][a-z0-9_]{0,63}$")
    label: str = Field(min_length=1, max_length=20)
    follow_up: FollowUpReason | None = None


class Item(Strict):
    item_id: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    type: QuestionType
    prompt: str = Field(min_length=1, max_length=400)
    options: list[Option] | None = Field(default=None, min_length=2, max_length=10)
    scale_min: int | None = Field(default=None, ge=0, le=100)
    scale_max: int | None = Field(default=None, ge=1, le=100)
    follow_up_at_or_below: int | None = Field(default=None, ge=0, le=100)
    follow_up_reason: FollowUpReason | None = None


class SendWindow(Strict):
    not_before: datetime  # IST, +05:30
    not_after: datetime


class SelfEval(Strict):
    schema_valid: bool | None = None
    relevance_score: float | None = Field(default=None, ge=0, le=1)
    gives_new_advice: bool | None = None
    asks_personal_info: bool | None = None


class Survey(Strict):
    survey_id: str = Field(pattern=r"^S-[0-9]{5,10}$")
    farmer_id: str
    source_insight_ids: list[str] = Field(min_length=1, max_length=5)
    language: str = Field(pattern=r"^[a-z]{2,3}-[A-Z]{2}$")
    send_window: SendWindow
    items: list[Item] = Field(min_length=1, max_length=3)
    rationale: str = Field(min_length=1, max_length=400)
    self_eval: SelfEval | None = None


class NoSurvey(Strict):
    insight_id: str
    farmer_id: str
    decision: Literal["no_survey"] = "no_survey"
    reason_code: Literal["generic_information", "needs_more_context"]
    reason: str = Field(max_length=300)


# ---------- scheduler hand-off ----------
class SendHours(Strict):
    from_: str = Field(alias="from")
    to: str


class ChannelStep(Strict):
    channel: Channel
    wait_hours: int | None


class FrequencyCap(Strict):
    key: str
    max_surveys: int
    window_days: int
    exempt: bool


class FarmerChatFlow(Strict):
    org_id: str
    flow_key: str
    version: int
    campaign_id: str
    flow_id: str


class ScheduleHandoff(Strict):
    handoff_id: str
    created_at: datetime
    survey: Survey
    send_window: SendWindow
    send_hours: SendHours
    channel_order: list[ChannelStep] = Field(min_length=1, max_length=5)
    frequency_cap: FrequencyCap
    priority: Literal["high", "normal", "low"]
    expires_at: datetime
    farmerchat_flow: FarmerChatFlow


# ---------- response (one survey attempt) ----------
class ResponseItem(Strict):
    item_id: str
    item_type: QuestionType
    result: Literal["answered", "skipped", "invalid", "not_reached", "discarded"]
    value: Union[int, str, None]
    raw_input: str | None
    input_kind: Literal["sheet_tap", "button_reply", "list_reply", "callback_data",
                        "keypad", "speech", "free_text"] | None
    attempts: int


class Response(BaseModel):
    """Answer record. Nested timestamps/farmerchat_reply stay loose; jsonschema checks them."""
    model_config = ConfigDict(extra="allow")
    response_key: str
    survey_id: str
    farmer_id: str
    source_insight_ids: list[str] = Field(min_length=1)
    channel: Channel
    language: str
    question_type: QuestionType | None = None
    status: Literal["completed", "partial", "negative_outcome", "expired", "declined",
                    "delivery_failed", "invalid_input", "stale", "duplicate", "identity_mismatch"]
    failure_code: str | None
    sheet_outcome: Literal["finished", "closed_partway", "interrupted", "dismissed", "never_opened"] | None
    answers: dict[str, Union[int, str]]  # {"q1": "yes", "q3": 4}
    items: list[ResponseItem] = Field(min_length=1, max_length=3)
    complete: bool
    delivery_attempt: int
    timestamps: dict
    recorded_at: datetime
    answered_by: str | None
    raw_event: str | None
    retry_decision: Literal["none", "retry_same_channel", "fall_back_to_next_channel", "give_up"]
    retry_after_minutes: int | None
    counts_toward_cap: bool
    farmerchat_reply: dict | None
    needs_follow_up: bool
    follow_up_reasons: list[FollowUpReason]


# ---------- bottom sheet (app / web presentation) ----------
class BottomSheet(BaseModel):
    model_config = ConfigDict(extra="forbid")
    presentation: dict
    sheet: dict


# ---------- farmer (data/pack/data/farmers.csv) ----------
class Farmer(Strict):
    farmer_id: str
    country: str
    state: str
    district: str
    language: str
    preferred_channel: Channel
    crops: list[str]
    land_size_band: str
    livestock: list[str]
    activity_level: str

    @classmethod
    def from_csv_row(cls, row: dict[str, str]) -> "Farmer":
        split = lambda s: [x for x in (s or "").split("|") if x]
        return cls(**{**row, "crops": split(row["crops"]), "livestock": split(row["livestock"])})


# ---------- jsonschema validation ----------
@cache
def _registry() -> tuple[Registry, dict[str, dict]]:
    schemas = {n: json.loads((SCHEMAS / f"{n}.schema.json").read_text()) for n in SCHEMA_NAMES}
    resources = []
    for name, s in schemas.items():
        r = Resource.from_contents(s)
        resources += [(s["$id"], r), (f"{name}.schema.json", r)]
    return Registry().with_resources(resources), schemas


def validator(name: str) -> Draft202012Validator:
    reg, schemas = _registry()
    return Draft202012Validator(schemas[name], registry=reg, format_checker=FormatChecker())


def validate(name: str, payload: dict) -> list[str]:
    """Return schema errors against docs/participant-pack/json/schemas/<name>.schema.json."""
    return [f"{'/'.join(map(str, e.absolute_path))}: {e.message}" for e in validator(name).iter_errors(payload)]
