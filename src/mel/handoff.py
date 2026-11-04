"""Deterministic scheduling instructions. No model call decides IDs, dates or caps.

This constructs the handoff only. A downstream scheduler enforces it at dispatch.
"""

from datetime import datetime, timedelta
from uuid import NAMESPACE_URL, uuid5

from mel.clock import parse_ist


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
        "created_at": parse_ist(now).replace(microsecond=0).isoformat(),
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
