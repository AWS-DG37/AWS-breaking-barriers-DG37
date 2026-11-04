"""Time zone shared by every timing decision. All pack dates are India Standard Time."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def parse_ist(value: str | datetime) -> datetime:
    """Return `value` as an India-Standard-Time-aware datetime.

    The insight contract (insight.schema.json) requires `created_at` to carry the
    +05:30 offset ("India time, always with the +05:30 offset"); survey windows use
    the same rule. A timezone-aware value (any offset, or a `Z`/UTC suffix) is
    converted to IST. A naive value has no defined instant, so it is rejected rather
    than interpreted against the machine's local timezone, which would make the same
    insight produce different windows on an IST laptop and a UTC ECS container.

    Raises ValueError for naive datetimes and for strings that are not ISO timestamps.
    """
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            # fromisoformat accepts a trailing "Z" from Python 3.11 onward.
            parsed = datetime.fromisoformat(value)
        except (TypeError, ValueError) as error:
            raise ValueError(f"not an ISO-8601 timestamp: {value!r}") from error
    if parsed.utcoffset() is None:
        raise ValueError(
            f"timestamp {value!r} has no timezone offset; "
            "pack timestamps must carry the +05:30 India offset"
        )
    return parsed.astimezone(IST)
