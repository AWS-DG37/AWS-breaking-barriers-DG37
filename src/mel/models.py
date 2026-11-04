"""LLM draft models and internal outcomes. Pack JSON Schemas remain the wire authority."""

from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import Field, model_validator

from mel.config import StrictModel

FollowUp = Literal[
    "advice_did_not_work",
    "crop_or_animal_damaged",
    "could_not_get_input",
    "could_not_reach_expert",
]


class Option(StrictModel):
    value: str = Field(pattern=r"^[a-z0-9][a-z0-9_]{0,63}$")
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


class ScaleItem(StrictModel):
    item_id: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    type: Literal["survey-scale"]
    prompt: str = Field(min_length=1, max_length=400)
    scale_min: int = Field(ge=0, le=100)
    scale_max: int = Field(ge=1, le=100)
    follow_up_at_or_below: int | None = None
    follow_up_reason: FollowUp | None = None

    @model_validator(mode="after")
    def check_range(self):
        if not 1 <= self.scale_max - self.scale_min <= 9:
            raise ValueError("scale must have two to ten integer steps")
        if self.follow_up_at_or_below is not None:
            if not self.scale_min <= self.follow_up_at_or_below <= self.scale_max:
                raise ValueError("follow-up threshold outside scale")
            if self.follow_up_reason is None:
                raise ValueError("follow-up threshold requires a reason")
        return self


Item = Annotated[ChoiceItem | ScaleItem, Field(discriminator="type")]


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
    items: list[Item] = Field(min_length=1, max_length=3)
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
    issues: list[str] = Field(
        description="Blocking defects that make this short survey misleading, unsafe, or unusable."
    )
    suggestions: list[str] = Field(
        default_factory=list,
        description="Optional improvements; do not block acceptance. Stay within three questions.",
    )


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
