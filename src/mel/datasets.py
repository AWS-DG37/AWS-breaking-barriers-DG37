"""Insight datasets: parsing JSONL input and the columns used to query an imported set."""

from __future__ import annotations

import json
from datetime import date, datetime

from pydantic import Field

from mel.config import StrictModel


class InsightFilter(StrictModel):
    """Selects insights within one dataset. Unset fields do not filter."""

    farmer_id: str | None = None
    crop: str | None = None
    concern: str | None = None
    resolution_type: str | None = None
    quality_flag: str | None = None
    created_from: date | None = None  # Inclusive, by India Standard Time day.
    created_to: date | None = None  # Inclusive, by India Standard Time day.
    limit: int | None = Field(default=None, ge=1)


def parse_insights(body: bytes, limit: int | None = None) -> list[dict]:
    if limit is not None and limit < 1:
        raise ValueError("limit must be at least 1")
    rows, seen = [], set()
    for line_number, line in enumerate(body.decode("utf-8-sig").splitlines(), 1):
        if not line.strip():
            continue
        row = json.loads(line)
        if not isinstance(row, dict):
            raise ValueError(f"line {line_number} is not an object")
        key = row.get("insight_id")
        if not isinstance(key, str) or not key:
            # Preserve malformed rows for rejection instead of crashing the batch.
            row = {**row, "insight_id": f"invalid-row-{line_number}", "quality_flag": "junk"}
        if row["insight_id"] in seen:
            raise ValueError(f"duplicate insight_id on line {line_number}")
        seen.add(row["insight_id"])
        rows.append(row)
        if limit is not None and len(rows) >= limit:
            break
    return rows


def index_columns(row: dict) -> dict:
    """Query columns for one insight. Junk rows keep whatever they have; the body is
    stored unchanged and the decision engine still judges the original row."""
    asset = row.get("asset")
    return {
        "insight_id": row["insight_id"],
        "farmer_id": _text(row.get("farmer_id")),
        "crop": _text(asset.get("name")) if isinstance(asset, dict) else None,
        "concern": _text(row.get("concern")),
        "resolution_type": _text(row.get("resolution_type")),
        "quality_flag": _text(row.get("quality_flag")),
        "created_at": _timestamp(row.get("created_at")),
    }


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.utcoffset() is not None else None
