"""Read-only view of the Bihar rabi crop calendar."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from datetime import date


class CropCalendar:
    def __init__(self, rows: Iterable[Mapping[str, str]]):
        self._rows: dict[str, list[dict]] = defaultdict(list)
        for row in rows:
            self._rows[row["crop"]].append(dict(row))

    def rows_for(self, crop: str | None) -> list[dict]:
        return list(self._rows.get(crop, [])) if crop else []

    def last_day(self, crop: str | None) -> date | None:
        """The crop's final calendar date (end of its last stage). None if the crop
        is not in the calendar, for example livestock."""
        rows = self.rows_for(crop)
        if not rows:
            return None
        return max(date.fromisoformat(row["end_date"]) for row in rows)
