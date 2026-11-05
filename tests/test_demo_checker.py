import functools
import http.server
import json
import threading
from pathlib import Path

import pytest
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
SURVEY = json.loads(
    (ROOT / "docs/voice/generated/openai-luna-I-004512-S-0000000001.json").read_text()
)
INSIGHT = {
    "insight_id": "I-000523",
    "conversation_language": "hi-IN",
    "asset": {"name": "cabbage"},
    "query_summary": "Caterpillars on cabbage",
    "advice_summary": "Stop spraying and check the leaves",
}


@pytest.fixture
def demo():
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(ROOT))
    handler.log_message = lambda *_args: None
    with http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler) as server:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                try:
                    page = browser.new_page()
                    started = []
                    page.route("**/api/agents/insights/*", lambda route: route.fulfill(
                        json={"insight": INSIGHT, "expected": None}))
                    page.route("**/api/agents/decide", lambda route: route.fulfill(
                        json={"decision": {"rule": "R8", "survey": True}}))

                    def runs(route):
                        if route.request.method == "POST":
                            started.append(route.request.post_data_json)
                            return route.fulfill(json={"run_id": "r1", "status": "running"})
                        route.fulfill(json={"status": "done", "result": {
                            "status": "accepted", "attempts": 1, "relevance": 0.9,
                            "language": SURVEY["language"], "items": SURVEY["items"]}})

                    page.route("**/api/agents/runs**", runs)
                    page.goto(f"http://127.0.0.1:{server.server_port}/docs/participant-pack/README.txt")
                    page.evaluate("""async base => {
                      const {showDemo} = await import(base + '/src/mel/explorer/static/demo.mjs');
                      document.body.replaceChildren();
                      const root = document.createElement('div');
                      document.body.append(root);
                      await showDemo(root);
                    }""", f"http://127.0.0.1:{server.server_port}")
                    yield page, started
                finally:
                    browser.close()
        finally:
            server.shutdown()


@pytest.mark.parametrize("choice,model,effort,name", [
    (None, "global.anthropic.claude-sonnet-5-5", None, "Claude Sonnet 5.5"),
    ("Low effort · Claude Sonnet 5.5", "global.anthropic.claude-sonnet-5-5", "low",
     "Claude Sonnet 5.5 (low effort)"),
    ("Smaller model · Claude Haiku 5.5", "global.anthropic.claude-haiku-5-5", None,
     "Claude Haiku 5.5"),
])
def test_demo_runs_the_chosen_checker_and_names_it(demo, choice, model, effort, name):
    page, started = demo
    checker = page.get_by_label("Checker")
    if choice:
        checker.select_option(label=choice)
    page.get_by_role("button", name="Run the follow-up").click()
    expect(page.locator(".demo-step").last).to_contain_text(
        f"approved by {name}", timeout=15000
    )
    assert started[-1]["verifier"] == model
    assert started[-1].get("verifier_effort") == effort
    assert started[-1]["generator"] == "global.anthropic.claude-opus-5-5"
