import importlib.util
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_builder():
    spec = importlib.util.spec_from_file_location("build_talk_diagram", ROOT / "scripts" / "build_talk_diagram.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_talk_slide_matches_the_diagram_source():
    builder = load_builder()
    expected = builder.embedded_svg(builder.SOURCE.read_text(encoding="utf-8"))
    page = (builder.TALK / "index.html").read_text(encoding="utf-8")
    assert builder.splice(page, expected) == page, "Run: uv run python scripts/build_talk_diagram.py"


def test_talk_has_every_icon_the_slide_uses_unchanged():
    builder = load_builder()
    svg = builder.embedded_svg(builder.SOURCE.read_text(encoding="utf-8"))
    names = builder.icon_names(svg)
    assert names
    for name in names:
        assert (builder.TALK / "aws-icons" / name).read_bytes() == (builder.ICONS / name).read_bytes(), name


def test_console_card_opens_the_live_console_from_the_talk():
    builder = load_builder()
    svg = builder.embedded_svg(builder.SOURCE.read_text(encoding="utf-8"))
    card = re.search(r'<g id="ad-console">(.*?)</g>', svg, re.S)
    assert card and "MEL Console" in card.group(1)
    page = (builder.TALK / "index.html").read_text(encoding="utf-8")
    assert '<script src="console-window.js' in page
    script = (builder.TALK / "console-window.js").read_text(encoding="utf-8")
    assert "getElementById('ad-console')" in script and "'/#demo'" in script
    # .cw sets display:flex, which would override [hidden] and leave a closed window on screen.
    styles = (builder.TALK / "styles.css").read_text(encoding="utf-8")
    assert ".cw[hidden]{display:none}" in styles


def test_diagram_classes_and_ids_are_namespaced():
    svg = re.search(r'<svg id="deployment".*?</svg>', (ROOT / "docs" / "aws-deployment.html").read_text(), re.S).group(0)
    classes = {c for attr in re.findall(r'class="([^"]+)"', svg) for c in attr.split()}
    ids = set(re.findall(r'\sid="([^"]+)"', svg)) - {"deployment"}
    assert classes and all(c.startswith("ad-") for c in classes), sorted(classes)
    assert ids and all(i.startswith("ad-") for i in ids), sorted(ids)
