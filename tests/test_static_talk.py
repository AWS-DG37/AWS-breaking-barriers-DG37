"""The static fieldecho.org snapshot stands alone: its files exist and it calls no server."""

import importlib.util
import re
from pathlib import Path

ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location(
    "build_static_talk", ROOT / "scripts/build_static_talk.py"
)
build_static_talk = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build_static_talk)


def test_snapshot_has_every_local_file_and_no_server_parts(tmp_path):
    build_static_talk.build(tmp_path)
    page = (tmp_path / "index.html").read_text()
    for ref in re.findall(r'(?:src|href)="(?!https:)([^"#?]+)', page):
        assert (tmp_path / ref.lstrip("/")).is_file(), ref
    module = (tmp_path / "static/demo-video.mjs").read_text()
    for ref in re.findall(r"'/(static/assets/[^']+)'", module):
        assert (tmp_path / ref).is_file(), ref
    for name in ("index.html", "app.js", "demo-card.js", "static/demo-video.mjs"):
        assert "/api/" not in (tmp_path / name).read_text(), name
    assert "deployment-banner" not in page
    assert "console-window" not in page
    assert not (tmp_path / "console-window.js").exists()
    assert "github.com/AWS-DG37/AWS-breaking-barriers-DG37" in page


def test_no_workflow_deploys_the_snapshot():
    for workflow in (ROOT / ".github/workflows").glob("*.yml"):
        text = workflow.read_text()
        assert "wrangler" not in text and "cloudflare" not in text.lower(), workflow.name
