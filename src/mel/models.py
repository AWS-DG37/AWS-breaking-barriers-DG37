"""LLM draft models and internal outcomes. Pack JSON Schemas remain the wire authority."""

from datetime import UTC, datetime
from typing import Literal

from pydantic import Field, model_validator

from mel.config import StrictModel

FollowUp = Literal[
    "advice_did_not_work",
    "crop_or_animal_damaged",
    "could_not_get_input",
    "could_not_reach_expert",
]

AnswerCode = Literal[
    "yes",
    "no",
    "not_yet",
    "better",
    "same",
    "worse",
    "all_steps",
    "some_steps",
    "not_started",
    "too_early",
    "cannot_tell",
    "did_not_check",
    "could_not_reach",
    "not_met_yet",
    "too_costly",
    "not_available",
    "no_time",
    "did_not_understand",
]


class Option(StrictModel):
    value: AnswerCode
    label: str = Field(min_length=1, max_length=20)
    follow_up: FollowUp | None = None


class ChoiceItem(StrictModel):
    item_id: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    type: Literal["survey-single-select", "survey-confirm"]
    prompt: str = Field(min_length=1, max_length=400)
    # The pack schema allows ten; golden questions never use more than four.
    options: list[Option] = Field(min_length=2, max_length=4)

    @model_validator(mode="after")
    def check_options(self):
        if self.type == "survey-confirm" and len(self.options) != 2:
            raise ValueError("survey-confirm requires exactly two options")
        if len({o.value for o in self.options}) != len(self.options):
            raise ValueError("option values must be unique within a question")
        return self


class TraceEvent(StrictModel):
    sequence: int = Field(ge=1)
    stage: str
    source: Literal["rule", "schema", "model", "runtime"]
    attempt: int = Field(ge=0)
    data: dict = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class QuestionEvidence(StrictModel):
    item_id: str
    measurement: Literal["adoption", "outcome", "barrier", "monitoring", "follow_up", "other"]
    source_fields: list[str]
    explanation: str


class SurveyDraft(StrictModel):
    question_evidence: list[QuestionEvidence] = Field(
        default_factory=list,
        description="Model-supplied diagnostic grounding. Not verified quality.",
    )
    items: list[ChoiceItem] = Field(min_length=1, max_length=3)
    rationale: str = Field(min_length=1, max_length=400)

    @model_validator(mode="after")
    def unique_ids(self):
        if len({i.item_id for i in self.items}) != len(self.items):
            raise ValueError("item IDs must be unique")
        ids = {item.item_id for item in self.items}
        if any(e.item_id not in ids for e in self.question_evidence):
            raise ValueError("question evidence must refer to a draft item")
        return self


class Review(StrictModel):
    relevance_score: float = Field(ge=0, le=1)
    language_correct: bool
    gives_new_advice: bool
    asks_personal_info: bool
    personal_info_quote: str | None = Field(
        default=None,
        description=(
            "The exact question or option text that asks for personal information; "
            "required when asks_personal_info is true, else null."
        ),
    )
    issues: list[str] = Field(
        description="Blocking defects that make this short survey misleading, unsafe, or unusable."
    )
    suggestions: list[str] = Field(
        default_factory=list,
        description="Optional improvements; do not block acceptance. Stay within three questions.",
    )

    @model_validator(mode="after")
    def require_personal_info_quote(self):
        if self.asks_personal_info and not (
            self.personal_info_quote and self.personal_info_quote.strip()
        ):
            raise ValueError("asks_personal_info requires a non-empty personal_info_quote")
        return self


class Outcome(StrictModel):
    events: list[TraceEvent] = Field(default_factory=list)
    question_evidence: list[QuestionEvidence] = Field(default_factory=list)
    insight_id: str
    status: Literal["accepted", "skipped", "rejected", "failed"]
    rule: str | None = None  # Golden rule code (R1-R8) that decided survey or not.
    reason: str | None = None
    detail: str | None = None
    survey: dict | None = None
    candidate_survey: dict | None = None  # Rejected draft for review; never a scheduler handoff.
    handoff: dict | None = None
    no_survey: dict | None = None
    review: Review | None = None
    attempts: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    usage_complete: bool = True
    elapsed_seconds: float = 0
    estimated_cost_usd: float | None = None
