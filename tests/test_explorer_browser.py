import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

import pytest


@pytest.mark.skipif(
    not os.environ.get("MEL_TEST_BROWSER"),
    reason="Set MEL_TEST_BROWSER=1 for the optional browser regression",
)
@pytest.mark.parametrize("saved_experiment", [True, False])
def test_sidebar_refresh_keeps_usage_visible_and_reloads_totals(saved_experiment):
    playwright_api = pytest.importorskip("playwright.sync_api")
    expect, sync_playwright = playwright_api.expect, playwright_api.sync_playwright
    static = Path("src/mel/explorer/static")
    calls = 2
    detail = {
        "experiment_id": "saved",
        "name": "Saved example",
        "source": "recorded",
        "status": "completed",
        "runs": [],
        "insight_count": 0,
        "candidate_count": 0,
        "spec": {},
    }

    def respond(route):
        path = urlsplit(route.request.url).path
        if path == "/":
            route.fulfill(path=static / "index.html", content_type="text/html")
            return
        if path.startswith("/static/"):
            kind = "text/javascript" if path.endswith(".mjs") else "text/css"
            route.fulfill(path=static / path.removeprefix("/static/"), content_type=kind)
            return
        responses = {
            "/api/health": {"database": "ready"},
            "/api/config": {},
            "/api/models": {"models": []},
            "/api/experiments": {"experiments": [detail] if saved_experiment else []},
            "/api/experiments/saved": detail,
            "/api/live-policy": {
                "version": 0,
                "scope": "Offline website requests",
                "effective": False,
                "enabled": False,
                "blockers": [],
                "prices": [],
                "pending_calls": 0,
                "unresolved_calls": 0,
                "budget_usd": "1",
                "remaining_usd": "1",
                "estimated_usd": "0",
                "held_usd": "0",
            },
            "/api/usage": {
                "metered": {
                    "calls": calls,
                    "complete_calls": calls,
                    "input_tokens": calls * 10,
                    "output_tokens": calls * 5,
                    "estimated_usd": "0",
                    "known_estimated_usd": "0",
                },
                "breakdown": [],
                "historical": {},
                "note": "Offline usage totals",
            },
        }
        assert path in responses, path
        route.fulfill(json=responses[path])

    with sync_playwright() as playwright:
        options = {}
        if not Path(playwright.chromium.executable_path).exists():
            if sys.platform != "darwin" or not Path("/Applications/Google Chrome.app").exists():
                pytest.skip("Install Playwright Chromium or Google Chrome for browser checks")
            options["channel"] = "chrome"
        with playwright.chromium.launch(**options) as browser:
            page = browser.new_page()
            page.route("**/*", respond)
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto("http://explorer.test/")
            if saved_experiment:
                expect(page.get_by_role("heading", name="Saved example")).to_be_visible()
            else:
                expect(page.get_by_text("No experiments saved yet.", exact=False)).to_be_visible()
            page.get_by_role("button", name="Usage and live controls", exact=True).click()
            usage = page.locator("#usage-panel")
            expect(usage.get_by_text("Website requests · 2.", exact=False)).to_be_visible()
            calls = 7
            page.get_by_role("button", name="Refresh", exact=True).click()
            expect(usage).to_be_visible()
            expect(usage.get_by_text("Website requests · 7.", exact=False)).to_be_visible()
            expect(page.locator("#content")).to_be_hidden()
            expect(page.locator("#composer")).to_be_hidden()
            assert errors == []
