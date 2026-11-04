"""Filters and sort orders for browsing saved runs and their results."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import Field

from mel.config import StrictModel

ResultStatus = Literal["accepted", "skipped", "rejected", "failed"]
# Result columns a table can sort by; "processed" is the order results were saved.
ResultSort = Literal["processed", "insight", "insight_date", "outcome", "relevance", "time"]
SortDirection = Literal["asc", "desc"]
RunStatus = Literal["running", "completed", "completed_with_errors", "failed"]
RunSort = Literal["newest", "oldest", "most_accepted", "most_results"]


class ResultFilter(StrictModel):
    """Selects and orders one run's results. Unset fields do not filter."""

    status: ResultStatus | None = None
    # Matched case-insensitively against the insight ID, the farmer's question, the
    # advice and every survey question (accepted or rejected draft).
    search: str | None = Field(default=None, min_length=1, max_length=200)
    crop: str | None = None
    concern: str | None = None
    rule: str | None = None
    sort: ResultSort = "processed"
    direction: SortDirection = "asc"


class RunFilter(StrictModel):
    """Selects and orders the Runs list. Unset fields do not filter."""

    status: RunStatus | None = None
    config_id: UUID | None = None
    sort: RunSort = "newest"


def like_pattern(text: str) -> str:
    """A substring ILIKE pattern that matches the text literally."""
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"
