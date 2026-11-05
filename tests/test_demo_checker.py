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


@pytest.mark.parametrize("choice,model,name", [
    (None, "global.anthropic.claude-sonnet-5-5", "Claude Sonnet 5.5"),
    ("Faster · Claude Haiku 5.5", "global.anthropic.claude-haiku-5-5", "Claude Haiku 5.5"),
])
def test_demo_runs_the_chosen_checker_and_names_it(demo, choice, model, name):
    page, started = demo
    checker = page.get_by_label("Checker")
    if choice:
        checker.select_option(label=choice)
    page.get_by_role("button", name="Run the follow-up").click()
    expect(page.locator(".demo-step").last).to_contain_text(
        f"approved by {name}", timeout=15000
    )
    assert started[-1]["verifier"] == model
    assert started[-1]["generator"] == "global.anthropic.claude-opus-5-5"
    assert "gate" not in started[-1]


SECOND_READS = [
    ({"status": "skipped", "decision_rule": "R3", "decision_survey": False,
      "decision_reason": "Second read: general information only."},
     "No survey after all", "No survey for this conversation"),
    ({"status": "failed", "decision_rule": None, "decision_survey": None,
      "error": "ValueError: classifier output was not JSON"},
     "The classifier did not decide", "No decision for this conversation"),
]


@pytest.mark.parametrize("second,step_text,verdict_text", SECOND_READS)
def test_a_classifier_second_read_that_says_no_or_fails_replaces_the_verdict(
    demo, second, step_text, verdict_text
):
    page, _ = demo
    sent = []

    def decide(route):
        sent.append(route.request.post_data_json)
        route.fulfill(json={"decision": {"rule": "R8", "survey": True, "gate": "classifier",
                                          "reason": "First read: advice to check."}})

    def runs(route):
        if route.request.method == "POST":
            sent.append(route.request.post_data_json)
            return route.fulfill(json={"run_id": "r2", "status": "running"})
        route.fulfill(json={"status": "done", "result": {"attempts": 0, **second}})

    page.route("**/api/agents/decide", decide)
    page.route("**/api/agents/runs**", runs)
    page.get_by_label("Who decides to follow up").select_option("classifier")
    page.get_by_role("button", name="Run the follow-up").click()
    expect(page.locator(".demo-step").first).to_contain_text("Decided by Claude Haiku 5.5")
    expect(page.locator(".demo-step").last).to_contain_text(step_text, timeout=15000)
    expect(page.locator(".demo-verdict")).to_contain_text(verdict_text)
    expect(page.get_by_text("We will follow up")).to_have_count(0)
    classifier = {"gate": "classifier", "classifier": "global.anthropic.claude-haiku-5-5"}
    assert [{k: body[k] for k in classifier} for body in sent] == [classifier, classifier]
