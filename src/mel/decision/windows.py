"""Send-window policies. A policy answers one question: when may this survey go out?"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, time, timedelta
from typing import Protocol

from mel.clock import IST
from mel.config import GoldenWindowConfig
from mel.decision.model import SendWindow


class WindowPolicy(Protocol):
    def window_for(self, insight: Mapping) -> SendWindow | None:
        """The send window, or None when no valid window fits inside the season."""


class PackWindowPolicy:
    """The participant pack's "When to send" rules (rules.json `send_window_days`,
    `window_rules`): days count from the insight's date, the window never opens on
    the advice day or before the action window ends, is at least `min_width_days`
    wide, and never runs past the season end."""

    def __init__(self, send_window_days: Mapping, window_rules: Mapping):
        self._offsets = send_window_days
        self._rules = window_rules
        self._season_end = date.fromisoformat(window_rules["season_end"])
        self._opens = time.fromisoformat(window_rules["not_before_time"])
        self._closes = time.fromisoformat(window_rules["not_after_time"])

    def window_for(self, insight: Mapping) -> SendWindow | None:
        rules = self._rules
        origin = datetime.fromisoformat(insight["created_at"]).astimezone(IST).date()
        action_days = insight["action_window_days"]
        if action_days is None:
            action_days = rules["action_window_days_if_null"]
        offsets = self._offsets[insight["verification"]["timing"]]
        if offsets == "from_action_window":
            start = action_days + rules["none_start_after_action_window_days"]
            end = action_days + rules["none_end_after_action_window_days"]
        else:
            start, end = offsets
        start = max(start, rules["earliest_start_day"])
        if rules["start_not_before_action_window_end"]:
            start = max(start, action_days)
        end = max(end, start + rules["min_width_days"])
        earliest = origin + timedelta(days=start)
        latest = min(origin + timedelta(days=end), self._season_end)
        if (latest - earliest).days < rules["min_width_days"]:
            return None
        return SendWindow(
            datetime.combine(earliest, self._opens, IST),
            datetime.combine(latest, self._closes, IST),
        )


class GoldenWindowPolicy:
    """The windows golden set v1 expects, inferred from its 95 surveys.

    Day-based timings open `days[timing][0]` days after the advice at `opens` and close
    `days[timing][1]` days after at `closes`, clipped to the season end; a window that
    would open after the season has none. The action window and minimum width are not
    applied. `same_day` opens `same_day_delay_hours` after the advice and lasts
    `same_day_length_hours`; when that opening falls outside `opens`-`closes`, it moves
    to the next `opens`.
    """

    def __init__(self, config: GoldenWindowConfig, season_end: date):
        self._days = config.days
        self._opens = time.fromisoformat(config.opens)
        self._closes = time.fromisoformat(config.closes)
        self._delay = timedelta(hours=config.same_day_delay_hours)
        self._length = timedelta(hours=config.same_day_length_hours)
        self._season_close = datetime.combine(season_end, self._closes, IST)

    def window_for(self, insight: Mapping) -> SendWindow | None:
        advised = datetime.fromisoformat(insight["created_at"]).astimezone(IST)
        timing = insight["verification"]["timing"]
        if timing == "same_day":
            start = self._next_opening(advised + self._delay)
            end = start + self._length
        elif timing in self._days:
            first, last = self._days[timing]
            start = datetime.combine(advised.date() + timedelta(days=first), self._opens, IST)
            end = min(
                datetime.combine(advised.date() + timedelta(days=last), self._closes, IST),
                self._season_close,
            )
        else:
            raise ValueError(f"golden_v1 window policy has no rule for timing {timing!r}")
        if start > self._season_close:
            return None
        return SendWindow(start, end)

    def _next_opening(self, moment: datetime) -> datetime:
        if moment.time() < self._opens:
            return datetime.combine(moment.date(), self._opens, IST)
        if moment.time() > self._closes:
            return datetime.combine(moment.date() + timedelta(days=1), self._opens, IST)
        return moment
