import json
from pathlib import Path

import pytest

from dg_agent.models import PACK


def jsonl(p: Path) -> list[dict]:
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


SCENARIOS = sorted((PACK / "json" / "scenarios").iterdir()) if PACK.exists() else []
OUTCOMES = sorted((PACK / "json" / "outcomes").rglob("*.json")) if PACK.exists() else []

pytestmark = pytest.mark.skipif(not PACK.exists(), reason="participant pack not in data/pack")
