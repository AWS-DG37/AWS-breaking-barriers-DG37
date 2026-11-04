"""Verify the recorded explorer through Chrome without starting inference."""

import argparse
import json
from pathlib import Path

from playwright.sync_api import expect, sync_playwright
from verify_architecture_viewer import exercise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8765")
    parser.add_argument("--screenshots", type=Path)
    parser.add_argument("--label", default="AWS-breaking-barriers-DG37 | Explorer prototype")
    args = parser.parse_args()
    with sync_playwright() as driver:
        browser = driver.chromium.launch(channel="chrome", headless=True)
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 1050})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(args.url)
            page.get_by_role("button", name="Import recorded run").click()
            page.get_by_role("heading", name="Recorded Bedrock comparison", exact=False).wait_for()
            candidate = page.get_by_label("Candidate", exact=True)
            expect(candidate.locator("option")).to_have_count(3)
            page.get_by_role("button", name="Review survey", exact=False).click()
            expect(page.get_by_role("heading", name="Reviewer findings")).to_be_visible()
            assert page.get_by_role("heading", name="Question preview").is_visible()
            phone = page.get_by_role("region", name="FarmerChat survey preview")
            expect(phone).to_be_visible()
            exercise(page, phone)
            expect(phone.get_by_text("1 / 2", exact=True)).to_be_visible()
            phone.get_by_role("radio").first.check()
            assert phone.get_by_role("radio").first.evaluate(
                "node => document.activeElement === node"
            )
            phone_height = phone.bounding_box()["height"]
            photo = phone.get_by_role("img", name="Mustard flowers in a field in Rajasthan, India")
            expect(photo).to_have_js_property("complete", True)
            assert photo.evaluate("image => image.complete && image.naturalWidth > 0")
            first_prompt = phone.locator("h3").inner_text()
            page.get_by_role("button", name="Question 2", exact=True).click()
            assert page.get_by_text("q2 · survey-single-select", exact=True).is_visible()
            expect(phone.get_by_text("2 / 2", exact=True)).to_be_visible()
            assert phone.locator("h3").inner_text() != first_prompt
            expect(phone.locator(".answers")).to_have_attribute("lang", "hi-IN")
            phone.get_by_role("radio").first.check()
            phone.get_by_role("button", name="पीछे", exact=True).click()
            expect(phone.get_by_role("radio").first).to_be_checked()
            expect(page.get_by_role("button", name="Question 1", exact=True)).to_have_attribute(
                "aria-pressed", "true"
            )
            expect(page.get_by_text("q1 · survey-single-select", exact=True)).to_be_visible()
            assert phone.bounding_box()["height"] == phone_height
            page.get_by_role("button", name="Review survey", exact=False).click()
            expect(phone.get_by_role("radio").first).to_be_checked()
            page.get_by_label("Reanalyze saved outputs").select_option("golden")
            page.get_by_text("Evaluation reference · golden.", exact=False).wait_for()
            page.get_by_label("Reanalyze saved outputs").select_option("rules")
            page.get_by_text("Evaluation reference · rules.", exact=False).wait_for()
            expect(phone.get_by_role("radio").first).to_be_checked()
            candidate = page.get_by_label("Candidate", exact=True)
            candidate.select_option("1")
            assert page.get_by_text("UnexpectedModelBehavior", exact=True).is_visible()
            assert page.get_by_text("No survey questions recorded for this outcome.").is_visible()
            expect(phone).to_have_count(0)
            candidate.select_option("0")
            page.get_by_role("button", name="Review survey", exact=False).click()
            expect(phone.get_by_role("button", name="शुरू करें", exact=True)).to_be_visible()
            phone.get_by_role("button", name="शुरू करें", exact=True).click()
            expect(phone.locator("input:checked")).to_have_count(0)
            if args.screenshots:
                args.screenshots.mkdir(parents=True, exist_ok=True)
                page.evaluate(
                    """label => {
                    const bar=document.createElement('div');
                    bar.textContent=label;
                    bar.style='padding:8px 16px;background:#001D09;color:white;font:14px system-ui';
                    document.body.prepend(bar);
                }""",
                    args.label,
                )
                page.screenshot(path=str(args.screenshots / "explorer-desktop.jpg"), full_page=True)
            assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
            page.set_viewport_size({"width": 390, "height": 844})
            assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
            if args.screenshots:
                page.screenshot(path=str(args.screenshots / "explorer-phone.jpg"), full_page=True)
            page.get_by_role("button", name="New live experiment").click()
            page.get_by_role("heading", name="New live experiment").wait_for()
            assert page.get_by_label("Reviewer model and native API").is_visible()
            assert not errors, errors
            print(
                json.dumps(
                    {
                        "result": "passed",
                        "candidates": 3,
                        "viewports": [1440, 390],
                        "model_calls_started": 0,
                        "browser_errors": errors,
                    }
                )
            )
        finally:
            browser.close()


if __name__ == "__main__":
    main()
