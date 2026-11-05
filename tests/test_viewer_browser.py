"""Exercise the shared login and viewer controls in a real browser, without AWS."""

import json
import socket
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from playwright.sync_api import expect, sync_playwright
from test_auth import VIEWER_PASSWORD, viewer_settings
from test_experiments import versions
from test_explorer_api import Store

from mel.explorer.api import create_app


class BrowserStore(Store):
    def list_configs(self):
        return list(self.configs.values())

    def list_runs(self, selection):
        return []

    def list_jobs(self):
        return []

    def list_review_lists(self):
        return []


@pytest.fixture
def viewer_server():
    store = BrowserStore()
    first, _ = versions(store)
    store.name_config(first, "Saved baseline")
    store.activate_config(first)

    async def no_catalog(_):
        pytest.fail("Viewing configuration must not discover models")

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        base = f"http://localhost:{listener.getsockname()[1]}"
        app = create_app(
            settings=viewer_settings(console_url=base),
            store_factory=lambda _: store,
            catalog_loader=no_catalog,
        )
        server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
        worker = threading.Thread(target=server.run, kwargs={"sockets": [listener]})
        worker.start()
        try:
            deadline = time.monotonic() + 10
            while not server.started and time.monotonic() < deadline:
                time.sleep(0.01)
            assert server.started, "Local test server did not start"
            yield base, first
        finally:
            server.should_exit = True
            worker.join(timeout=10)
            assert not worker.is_alive(), "Local test server failed to stop"


@pytest.mark.parametrize("width", [1440, 390])
def test_shared_password_login_browsing_and_write_controls(viewer_server, width):
    base, config_id = viewer_server
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": width, "height": 900})
            errors = []
            agent_requests = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on(
                "request",
                lambda request: (
                    agent_requests.append(request.url) if "/api/agents/" in request.url else None
                ),
            )
            page.goto(base + "/")
            expect(page.get_by_role("heading", name="Sign in")).to_be_visible()
            expect(page.get_by_role("link", name="Sign in for full access")).to_be_visible()
            page.get_by_label("Viewer password").fill("wrong")
            page.get_by_role("button", name="View only").click()
            expect(page.get_by_role("alert")).to_contain_text("Incorrect viewer password")
            page.get_by_label("Viewer password").fill(VIEWER_PASSWORD)
            page.get_by_role("button", name="View only").click()
            expect(page.get_by_role("heading", name="Runs", exact=True)).to_be_visible()
            expect(page.get_by_text("View-only access.", exact=False)).to_be_visible()
            expect(page.get_by_role("link", name="Generate", exact=True)).to_be_hidden()
            for tab in ["Agents", "Demo"]:
                expect(page.get_by_role("link", name=tab, exact=True)).to_be_hidden()
                page.evaluate("tab => location.hash='#'+tab.toLowerCase()", tab)
                expect(page).to_have_url(base + "/#runs")
            assert agent_requests == []
            page.goto(base + f"/#configure/{config_id}/edit")
            expect(page.get_by_role("heading", name="Saved baseline", exact=True)).to_be_visible()
            expect(page.get_by_role("link", name="Edit as draft")).to_have_count(0)
            expect(page.get_by_role("button", name="Make active")).to_have_count(0)
            assert (
                page.evaluate(
                    "fetch('/api/jobs/batch', {method:'POST', headers:"
                    "{'Content-Type':'application/json'}, body:'{}'})"
                    ".then(r => r.status)"
                )
                == 403
            )
            page.evaluate("location.hash='#compare'")
            expect(page.get_by_role("button", name="Refresh", exact=True)).to_be_visible()
            expect(page.get_by_role("button", name="New comparison", exact=True)).to_be_hidden()
            expect(page.get_by_role("button", name="Import recorded run")).to_be_hidden()
            page.evaluate("location.hash='#review'")
            expect(page.get_by_role("heading", name="Review lists")).to_be_visible()
            expect(page.get_by_role("button", name="Create list")).to_have_count(0)
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            page.get_by_role("button", name="Sign out", exact=True).click()
            expect(page.get_by_role("heading", name="Sign in")).to_be_visible()
            assert page.request.get(base + "/api/experiments").status == 401
            assert errors == []
        finally:
            browser.close()


def test_viewer_can_preview_a_saved_survey_without_mutation_controls(viewer_server):
    base, _ = viewer_server
    run_id = "00000000-0000-0000-0000-000000000001"
    recorded = json.loads(Path("src/mel/explorer/demo/recorded-run.json").read_text())
    pair = recorded["runs"][0]["results"][0]
    insight_id = pair["input"]["insight_id"]
    outcome = pair["outcome"]
    row = {"insight_id": insight_id, "status": outcome["status"], "insight": pair["input"]}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page()
            fixtures = {
                f"/api/runs/{run_id}": {
                    "run_id": run_id,
                    "status": "completed",
                    "counts": {"accepted": 1},
                },
                f"/api/runs/{run_id}/results": {"results": [row]},
                f"/api/runs/{run_id}/facets": {"crops": {}, "concerns": {}, "rules": {}},
                f"/api/runs/{run_id}/results/{insight_id}": {**row, "outcome": outcome},
            }
            # These saved read responses stand in for PostgreSQL, while login and
            # authorization use the running application above.
            page.route(
                "**/api/runs/**",
                lambda route: route.fulfill(
                    json=fixtures[route.request.url.removeprefix(base).split("?")[0]]
                ),
            )
            page.goto(base + f"/static/sign-in.html#runs/run/{run_id}/{insight_id}")
            page.get_by_label("Viewer password").fill(VIEWER_PASSWORD)
            page.get_by_role("button", name="View only").click()
            expect(page.get_by_role("region", name="FarmerChat survey preview")).to_be_visible()
            expect(page.get_by_role("button", name="Delete run")).to_have_count(0)
            expect(page.get_by_text("Add to review list", exact=True)).to_have_count(0)
            assert "null" not in page.locator(".gen-detail").inner_text()
        finally:
            browser.close()
