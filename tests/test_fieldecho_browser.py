import http.client
import os
import time
import urllib.error
import urllib.request

import pytest


@pytest.mark.skipif(
    not os.environ.get("FIELDECHO_BROWSER_URL"), reason="CI image URL not configured"
)
def test_current_console_recorded_comparison_without_model_catalog():
    from playwright.sync_api import expect, sync_playwright

    base = os.environ["FIELDECHO_BROWSER_URL"]
    for attempt in range(60):
        try:
            with urllib.request.urlopen(base + "/api/health", timeout=2) as response:
                assert b'"database":"connected"' in response.read()
            break
        except (urllib.error.URLError, http.client.RemoteDisconnected, AssertionError):
            if attempt == 59:
                raise
            time.sleep(1)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.route("**/api/models", lambda route: route.fulfill(json={"models": []}))
            page.route("https://**/*", lambda route: route.abort())
            page.goto(base + "/#compare")
            expect(page.get_by_role("button", name="New comparison", exact=True)).to_be_visible()
            page.get_by_role("button", name="Import recorded run", exact=True).click()
            expect(page.get_by_label("Version", exact=True)).to_be_visible(timeout=30000)
            expect(page.get_by_label("Insight", exact=True)).to_be_visible()
            assert errors == []
        finally:
            browser.close()
