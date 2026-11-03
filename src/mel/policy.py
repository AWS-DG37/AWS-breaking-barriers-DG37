"""Deterministic scheduling instructions. No model call decides IDs, dates or caps.

This constructs the handoff only. A downstream scheduler enforces it at dispatch.
"""

from datetime import datetime, time, timedelta
from uuid import NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def eligibility(insight: dict, rules: dict) -> str | None:
    if insight.get("quality_flag") == "junk":
        return "junk_insight"
    resolution = insight["resolution_type"]
    if not rules["survey_by_resolution"][resolution]["survey"]:
        return resolution
    if not insight["verification"]["needed"]:
        return "verification_not_needed"
    return None


def send_window(insight: dict, rules: dict) -> dict:
    policy = rules["window_rules"]
    origin = datetime.fromisoformat(insight["created_at"]).astimezone(IST).date()
    action_days = insight["action_window_days"]
    if action_days is None:
        action_days = policy["action_window_days_if_null"]
    offsets = rules["send_window_days"][insight["verification"]["timing"]]
    if offsets == "from_action_window":
        start = action_days + policy["none_start_after_action_window_days"]
        end = action_days + policy["none_end_after_action_window_days"]
    else:
        start, end = offsets
    start = max(start, policy["earliest_start_day"])
    if policy["start_not_before_action_window_end"]:
        start = max(start, action_days)
    end = max(end, start + policy["min_width_days"])
    earliest = origin + timedelta(days=start)
    latest = min(origin + timedelta(days=end), datetime.fromisoformat(policy["season_end"]).date())
    if (latest - earliest).days < policy["min_width_days"]:
        raise ValueError("no_valid_season_window")
    return {
        "not_before": datetime.combine(
            earliest, time.fromisoformat(policy["not_before_time"]), IST
        ).isoformat(),
        "not_after": datetime.combine(
            latest, time.fromisoformat(policy["not_after_time"]), IST
        ).isoformat(),
    }


def handoff(survey: dict, insight: dict, rules: dict, now: datetime) -> dict:
    p = rules["priority"]
    if (
        insight["resolution_type"] in p["high_if_resolution"]
        or insight["severity"] in p["high_if_severity"]
        or insight["behavioral_intent"] in p["high_if_behavioral_intent"]
    ):
        priority = "high"
    elif insight["resolution_type"] in p["low_if_resolution"]:
        priority = "low"
    else:
        priority = p["otherwise"]
    candidates = [
        insight["preferred_channel"],
        rules["channel_by_method"][insight["verification"]["method"]],
        "whatsapp",
    ]
    if priority == "high":
        candidates.append("voice")
    channels = list(dict.fromkeys(c for c in candidates if c))
    cap = rules["frequency_cap"]
    flow = rules["farmerchat_flow"]
    return {
        "handoff_id": survey["survey_id"].replace("S-", "H-", 1),
        "created_at": now.astimezone(IST).replace(microsecond=0).isoformat(),
        "survey": survey,
        "send_window": survey["send_window"],
        "send_hours": rules["send_hours"],
        "channel_order": [
            {
                "channel": c,
                "wait_hours": rules["wait_hours_by_priority"][priority]
                if index < len(channels) - 1
                else None,
            }
            for index, c in enumerate(channels)
        ],
        "frequency_cap": {
            "key": cap["key"].format(farmer_id=insight["farmer_id"]),
            "max_surveys": cap["max_surveys"],
            "window_days": cap["window_days"],
            "exempt": priority in cap["exempt_priority"],
        },
        "priority": priority,
        "expires_at": (
            datetime.fromisoformat(survey["send_window"]["not_after"])
            + timedelta(days=rules["expiry_days_after_window"])
        ).isoformat(),
        "farmerchat_flow": {
            "org_id": flow["org_id"],
            "flow_key": flow["flow_key"].format(
                survey_id_lower=survey["survey_id"].lower().replace("-", "_")
            ),
            "version": flow["version"],
            "campaign_id": str(uuid5(NAMESPACE_URL, flow["campaign_name"])),
            "flow_id": str(uuid5(NAMESPACE_URL, f"{flow['org_id']}/{survey['survey_id']}")),
        },
    }
