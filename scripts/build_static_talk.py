"""Build the static fieldecho.org snapshot: the talk deck and demo video, nothing else.

The hackathon's AWS hosting is gone. This copies src/mel/explorer/talk/ to the root of OUTPUT with
the few /static/ files it needs, and removes what needs a server: the deployment banner (it calls
/api/) and slide 4's live console window. On the snapshot, slide 4's console card opens the demo
video instead (talk/demo-card.js).

No workflow runs this, so commits never change the site. To publish a new snapshot by hand:

    uv run python scripts/build_static_talk.py dist/talk-site
    npx wrangler deploy --name fieldecho --assets dist/talk-site \
      --compatibility-date 2026-11-01 --domain fieldecho.org --domain www.fieldecho.org
"""

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXPLORER = ROOT / "src" / "mel" / "explorer"
TALK = EXPLORER / "talk"
STATIC = EXPLORER / "static"
STATIC_FILES = (
    "demo-video.css",
    "demo-video.mjs",
    "assets/favicon.ico",
    "assets/icon-192.png",
    "assets/apple-touch-icon.png",
    "assets/demo-video.mp4",
    "assets/demo-video-poster.jpg",
)
REMOVED_LINES = (
    '<link rel="stylesheet" href="/static/deployment-banner.css">',
    '<script type="module" src="/static/deployment-banner.mjs"></script>',
)
CONSOLE_SCRIPT = '<script src="console-window.js?v=2"></script>'
CARD_SCRIPT = '<script src="demo-card.js?v=1"></script>'


def static_index(html: str) -> str:
    """The deck's page without the parts that need the hosted console."""
    for line in (*REMOVED_LINES, CONSOLE_SCRIPT):
        if line not in html:
            raise ValueError(f"talk/index.html no longer contains: {line}")
    for line in REMOVED_LINES:
        html = html.replace(line + "\n", "")
    return html.replace(CONSOLE_SCRIPT, CARD_SCRIPT)


def build(output: Path) -> None:
    if output.exists():
        shutil.rmtree(output)
    shutil.copytree(TALK, output, ignore=shutil.ignore_patterns("console-window.js"))
    index = output / "index.html"
    index.write_text(static_index(index.read_text()))
    for name in STATIC_FILES:
        target = output / "static" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(STATIC / name, target)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: build_static_talk.py OUTPUT_DIR")
    build(Path(sys.argv[1]))
    print(f"built {sys.argv[1]}")
