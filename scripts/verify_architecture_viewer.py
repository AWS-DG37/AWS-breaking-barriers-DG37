"""Exercise the static delivery simulator in Chrome, without inference."""

import argparse
import functools
import http.server
import json
import tempfile
import threading
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]


def exercise(page, phone):
    phone.get_by_role("button", name="शुरू करें", exact=True).click()
    next_button = phone.get_by_role("button", name="आगे", exact=True)
    expect(next_button).to_be_disabled()
    radios = phone.get_by_role("radio")
    radios.first.focus()
    page.keyboard.press("Space")
    expect(radios.first).to_be_checked()
    assert phone.locator(".answers label").evaluate_all(
        """nodes => nodes.every(node =>
            getComputedStyle(node).backgroundColor === 'rgb(255, 255, 255)')"""
    )
    expect(radios.first).to_be_focused()
    page.keyboard.press("ArrowDown")
    expect(radios.nth(1)).to_be_checked()
    next_button.click()
    expect(phone.locator("h3")).to_be_focused()
    phone.get_by_role("button", name="पीछे", exact=True).click()
    expect(phone.get_by_role("radio").nth(1)).to_be_checked()
    next_button.click()
    phone.get_by_role("radio").first.check()
    phone.get_by_role("button", name="पूरा करें", exact=True).click()
    expect(phone.get_by_role("heading", name="धन्यवाद")).to_be_visible()
    phone.get_by_role("button", name="फिर से शुरू करें").click()
    phone.get_by_role("button", name="शुरू करें", exact=True).click()
    expect(phone.locator("input:checked")).to_have_count(0)
    expect(phone.get_by_role("button", name="आगे", exact=True)).to_be_disabled()


def check(url, screenshots=None):
    with sync_playwright() as driver:
        browser = driver.chromium.launch(channel="chrome", headless=True)
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 1050})
            requests, errors = [], []
            page.on("request", lambda request: requests.append((request.method, request.url)))
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(url)
            phone = page.get_by_role("region", name="FarmerChat survey preview")
            expect(phone).to_be_visible()
            photo = phone.get_by_role("img")
            expect(photo).to_have_js_property("complete", True)
            assert photo.evaluate("node => node.naturalWidth > 0")
            exercise(page, phone)
            selector = page.get_by_label("Recorded candidate", exact=True)
            selector.select_option("1")
            expect(phone).to_have_count(0)
            expect(
                page.get_by_text("No survey questions recorded for this outcome.")
            ).to_be_visible()
            expect(page.get_by_text("UnexpectedModelBehavior", exact=False)).to_be_visible()
            selector.select_option("2")
            expect(phone.get_by_role("button", name="शुरू करें", exact=True)).to_be_visible()
            selector.select_option("0")
            expect(phone.get_by_role("button", name="शुरू करें", exact=True)).to_be_visible()
            phone.get_by_role("button", name="शुरू करें", exact=True).click()
            for width in (1440, 390):
                page.set_viewport_size({"width": width, "height": 844})
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                if screenshots:
                    screenshots.mkdir(parents=True, exist_ok=True)
                    phone.screenshot(path=str(screenshots / f"phone-{width}.png"))
            test_survey = {
                "language": "en",
                "items": [
                    {
                        "item_id": "zero",
                        "type": "survey-scale",
                        "prompt": "Pick a number",
                        "scale_min": 0,
                        "scale_max": 100,
                    },
                    {
                        "item_id": "confirm",
                        "type": "survey-confirm",
                        "prompt": "Confirm?",
                        "options": [
                            {"label": "Yes", "value": "yes"},
                            {"label": "No", "value": "no"},
                        ],
                    },
                ],
            }
            catalog_route = "**/explorer-preview/surveys.json"
            test_catalog = {
                "candidates": [
                    {
                        "label": "TEST ONLY scale and confirm",
                        "status": "rejected",
                        "insight_id": "TEST ONLY",
                        "candidate_survey": test_survey,
                        "question_evidence": [],
                    }
                ]
            }
            page.route(catalog_route, lambda route: route.fulfill(json=test_catalog))
            page.reload()
            expect(page.get_by_text("Rejected draft.", exact=False)).to_be_visible()
            phone.get_by_role("button", name="Start", exact=True).click()
            phone.get_by_role("radio", name="0", exact=True).check()
            phone.get_by_role("button", name="Next", exact=True).click()
            phone.get_by_role("radio", name="Yes", exact=True).check()
            phone.get_by_role("button", name="Finish", exact=True).click()
            expect(phone.get_by_role("heading", name="Thank you")).to_be_visible()
            page.unroute(catalog_route)
            page.evaluate(
                """async survey => {
                const {mountSurveyPlayer} = await import('./explorer-preview/survey-player.mjs');
                window.testSession = {phase:'intro',question:0,answers:{}};
                window.testPlayer = mountSurveyPlayer(document.querySelector('#recorded-phone'), {
                    session: window.testSession, photoUrl:'./explorer-preview/mustard-field.webp',
                    survey});
            }""",
                test_survey,
            )
            phone.get_by_role("button", name="Start", exact=True).click()
            assert phone.evaluate(
                """node => {
                    const footer = node.querySelector('.player-navigation').getBoundingClientRect();
                    return footer.bottom <= node.getBoundingClientRect().bottom;
                }"""
            )
            expect(phone.get_by_role("radio")).to_have_count(101)
            assert phone.locator(".player-body").evaluate(
                "node => node.scrollHeight > node.clientHeight"
            )
            page.keyboard.press("Enter")
            expect(phone.get_by_role("heading", name="Pick a number")).to_be_visible()
            page.evaluate("window.testPlayer.showQuestion(1)")
            phone.get_by_role("radio", name="Yes", exact=True).check()
            phone.get_by_role("button", name="Finish", exact=True).click()
            expect(phone.get_by_role("heading", name="Pick a number")).to_be_visible()
            phone.get_by_role("radio", name="0", exact=True).check()
            assert page.evaluate("window.testSession.answers.zero === 0")
            phone.locator(".player-body").evaluate("node => node.scrollTop = node.scrollHeight")
            phone.get_by_role("button", name="Next", exact=True).click()
            phone.get_by_role("button", name="Finish", exact=True).click()
            expect(phone.get_by_role("heading", name="Thank you")).to_be_visible()
            phone.get_by_role("button", name="Restart", exact=True).click()
            assert page.evaluate("Object.keys(window.testSession.answers).length === 0")
            page.route(catalog_route, lambda route: route.abort())
            page.reload()
            expect(page.get_by_role("alert")).to_contain_text("Recorded preview unavailable")
            page.unroute(catalog_route)
            page.get_by_role("button", name="Retry loading preview").click()
            expect(phone).to_be_visible()
            assert not errors, errors
            assert all(method == "GET" and "/api" not in target for method, target in requests), (
                requests
            )
            print(
                json.dumps(
                    {
                        "result": "passed",
                        "viewports": [1440, 390],
                        "browser_errors": errors,
                        "non_get_requests": 0,
                    }
                )
            )
        finally:
            browser.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url")
    parser.add_argument("--screenshots", type=Path)
    args = parser.parse_args()
    if args.url:
        check(args.url, args.screenshots)
        return
    from build_architecture_viewer import build

    with tempfile.TemporaryDirectory() as folder:
        build(Path(folder))
        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=folder)
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            check(f"http://127.0.0.1:{server.server_port}/", args.screenshots)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    main()
