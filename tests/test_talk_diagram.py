"""The AWS deployment diagram (docs/aws-deployment.html) can be embedded in other pages.

The talk's slide content is not tested: people edit the slides by hand. After a change to
the diagram, run `uv run python scripts/build_talk_diagram.py` to refresh slide 4.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_diagram_classes_and_ids_are_namespaced():
    page = (ROOT / "docs" / "aws-deployment.html").read_text()
    svg = re.search(r'<svg id="deployment".*?</svg>', page, re.S).group(0)
    classes = {c for attr in re.findall(r'class="([^"]+)"', svg) for c in attr.split()}
    ids = set(re.findall(r'\sid="([^"]+)"', svg)) - {"deployment"}
    assert classes and all(c.startswith("ad-") for c in classes), sorted(classes)
    assert ids and all(i.startswith("ad-") for i in ids), sorted(ids)
