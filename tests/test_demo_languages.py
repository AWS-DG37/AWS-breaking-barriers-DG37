"""Exercise independent demo languages in the real browser players."""

import functools
import http.server
import json
import threading
from pathlib import Path

import pytest
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
GENERATED = "openai-luna-I-004512-S-0000000001.json"


@pytest.fixture
def demo_page():
    class QuietHandler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *_args):
            pass

    handler = functools.partial(QuietHandler, directory=str(ROOT))
    with http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                try:
                    page = browser.new_page()
                    page.set_default_timeout(3000)
                    # Speech is an external browser service; capture its requested language/text.
                    page.add_init_script("""
                      window.spoken = [];
                      window.utterances = [];
                      window.cancelled = 0;
                      window.SpeechSynthesisUtterance = class {
                        constructor(text) { this.text = text; }
                      };
                      Object.defineProperty(window, 'speechSynthesis', {value: {
                        cancel() { window.cancelled++; },
                        speak(line) {
                          window.spoken.push({text: line.text, lang: line.lang});
                          window.utterances.push(line);
                        }
                      }});
                    """)
                    page.route("https://**/*", lambda route: route.abort())
                    page.route("**/audio/*.mp3", lambda route: route.abort())
                    yield page, f"http://127.0.0.1:{server.server_port}"
                finally:
                    browser.close()
        finally:
            server.shutdown()
            thread.join()


def test_voice_demo_changes_text_and_audio_independently_without_losing_answers(demo_page):
    page, base = demo_page
    page.goto(f"{base}/docs/voice/?scenario=gen:{GENERATED}")
    expect(page.locator("#card h2")).to_have_text("क्या आपने अनुशंसित कीटनाशक का छिड़काव किया?")
    page.get_by_role("button", name="हाँ", exact=True).click()
    before = json.loads(page.locator("#record").inner_text())
    page.get_by_role("checkbox", name="English text", exact=True).check()
    expect(page.locator("#card h2")).to_have_text("Did you spray the recommended insecticide?")
    expect(page.get_by_role("button", name="Yes", exact=True)).to_have_attribute(
        "aria-pressed", "true"
    )
    expect(page.get_by_role("checkbox", name="English audio", exact=True)).not_to_be_checked()
    page.get_by_role("button", name="Next", exact=True).click()
    page.get_by_role("checkbox", name="English text", exact=True).uncheck()
    expect(page.locator("#card h2")).to_contain_text("सरसों")
    page.get_by_role("checkbox", name="English audio", exact=True).check()
    page.locator("#voice").click()
    page.wait_for_function("window.spoken.length > 0")
    assert page.evaluate("window.spoken.at(-1).lang") == "en-IN"
    assert page.evaluate("window.spoken.at(-1).text").startswith(
        "How are the green insects on the mustard flowers and stems now?"
    )
    after = json.loads(page.locator("#record").inner_text())
    assert after["answers"] == before["answers"] == {"q1": "yes"}
    assert after["language"] == "hi-IN"
    assert after["items"][0]["raw_input"] == "हाँ"


def test_voice_demo_waits_for_survey_before_enabling_controls(demo_page):
    page, base = demo_page
    errors = []
    pending = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.route(f"**/generated/{GENERATED}", lambda route: pending.append(route))
    for visit in (lambda: page.goto(f"{base}/docs/voice/?scenario=gen:{GENERATED}"),
                  page.reload):
        visit()
        page.wait_for_function("document.querySelector('#scenario').options.length > 0")
        expect(page.locator("#english-text")).to_be_disabled()
        expect(page.locator("#english-audio")).to_be_disabled()
        expect(page.locator("#voice")).to_be_disabled()
        assert pending
        pending.pop().fulfill(
            path=ROOT / "docs/voice/generated" / GENERATED,
            content_type="application/json",
        )
        page.get_by_role("checkbox", name="English text", exact=True).check()
        expect(page.locator("#card h2")).to_have_text("Did you spray the recommended insecticide?")
        expect(page.locator("#voice")).to_be_enabled()
    assert errors == []


def test_voice_demo_disables_controls_during_replacement_and_failed_load(demo_page):
    page, base = demo_page
    page.goto(f"{base}/docs/voice/?scenario=gen:{GENERATED}")
    page.get_by_role("checkbox", name="English text", exact=True).check()
    pending = []
    page.route("**/survey.*-IN.json", lambda route: pending.append(route))
    page.locator("#scenario").select_option("ref:1-mustard-aphid-spray")
    expect(page.locator("#english-text")).to_be_disabled()
    expect(page.locator("#english-audio")).to_be_disabled()
    expect(page.locator("#voice")).to_be_disabled()
    page.wait_for_timeout(50)
    assert pending
    pending.pop().fulfill(status=503, body="unavailable")
    expect(page.get_by_role("alert")).to_contain_text("Could not load the survey (503)")
    expect(page.locator("#english-text")).to_be_disabled()
    expect(page.locator("#voice")).to_be_disabled()
    page.locator("#scenario").select_option(f"gen:{GENERATED}")
    expect(page.locator("#english-text")).to_be_enabled()
    expect(page.locator("#english-text")).to_be_checked()
    expect(page.locator("#english-audio")).not_to_be_checked()


@pytest.mark.parametrize("language,native_english", [("bn-IN", False), ("en-IN", True)])
def test_voice_demo_native_english_controls_follow_survey_metadata(
    demo_page, language, native_english
):
    page, base = demo_page
    survey = json.loads((ROOT / "docs/voice/generated" / GENERATED).read_text())
    survey["language"] = language
    page.route(base + "/custom-survey.json", lambda route: route.fulfill(json=survey))
    page.goto(f"{base}/docs/voice/?survey=/custom-survey.json")
    expect(page.locator("#card h2")).to_have_text(survey["items"][0]["prompt"])
    for control in ("#english-text", "#english-audio"):
        expect(page.locator(control)).to_be_disabled()
        expect(page.locator(control)).to_be_checked(checked=native_english)
    expect(page.locator("#translation-note")).to_have_text(
        "This survey is already in English." if native_english else
        "English demo translation is unavailable for this survey."
    )


def mount_console_player(page, base, survey):
    page.goto(base + "/docs/participant-pack/README.txt")
    page.evaluate("""async ({survey, base}) => {
      const {mountSurveyPlayer} = await import(base + '/src/mel/explorer/static/survey-player.mjs');
      document.body.replaceChildren();
      const host = document.createElement('div');
      host.id = 'player';
      host.className = 'survey-player';
      document.body.append(host);
      window.session = {phase: 'questions', question: 0, answers: {}};
      mountSurveyPlayer(host, {survey, session: window.session, photoUrl: ''});
    }""", {"survey": survey, "base": base})


def test_console_demo_preserves_progress_and_cancels_previous_audio(demo_page):
    page, base = demo_page
    survey = json.loads((ROOT / "docs/voice/generated" / GENERATED).read_text())
    mount_console_player(page, base, survey)
    page.get_by_role("radio", name="हाँ", exact=True).check()
    page.get_by_role("checkbox", name="English text", exact=True).check()
    expect(page.get_by_role("radio", name="Yes", exact=True)).to_be_checked()
    page.get_by_role("button", name="Next", exact=True).click()
    page.get_by_role("checkbox", name="English audio", exact=True).check()
    page.get_by_role("button", name="Read aloud", exact=True).click()
    assert page.evaluate("window.spoken.at(-1).lang") == "en-IN"
    cancelled = page.evaluate("window.cancelled")
    page.get_by_role("checkbox", name="English audio", exact=True).uncheck()
    assert page.evaluate("window.cancelled") > cancelled
    assert page.evaluate("window.session.question") == 1
    assert page.evaluate("window.session.answers") == {"q1": "yes"}
    expect(page.locator(".player-body h3")).to_have_text(
        "How are the green insects on the mustard flowers and stems now?"
    )


def test_unknown_survey_does_not_offer_an_incorrect_translation(demo_page):
    page, base = demo_page
    survey = json.loads((ROOT / "docs/voice/generated" / GENERATED).read_text())
    survey["items"][0]["prompt"] = "नया सवाल"
    mount_console_player(page, base, survey)
    expect(page.get_by_role("checkbox", name="English text", exact=True)).to_be_disabled()
    expect(page.get_by_role("checkbox", name="English audio", exact=True)).to_be_disabled()
    expect(page.get_by_text(
        "English demo translation is unavailable for this survey."
    )).to_be_visible()
    expect(page.locator(".player-body h3")).to_have_text("नया सवाल")


def test_voice_demo_reacts_to_english_answers_and_switches_back_after_completion(demo_page):
    page, base = demo_page
    page.goto(f"{base}/docs/voice/?scenario=gen:{GENERATED}")
    page.get_by_role("checkbox", name="English text", exact=True).check()
    page.get_by_role("checkbox", name="English audio", exact=True).check()
    page.get_by_role("button", name="Could not get the insecticide", exact=True).click()
    expect(page.locator(".follow")).to_have_text("FarmerChat will follow up on this answer.")
    page.get_by_role("button", name="Next", exact=True).click()
    page.locator("#voice").click()
    page.wait_for_function("window.spoken.length > 0")
    page.get_by_role("button", name="More", exact=True).click()
    page.get_by_role("button", name="Finish", exact=True).click()
    expect(page.locator("#card h2")).to_have_text("Thank you")
    page.wait_for_function("window.spoken.at(-1).text.includes('contact you again')")
    assert page.evaluate("window.spoken.at(-1).lang") == "en-IN"
    record = json.loads(page.locator("#record").inner_text())
    assert record["status"] == "negative_outcome"
    assert record["language"] == "hi-IN"
    assert record["items"][0]["raw_input"] == "कीटनाशक नहीं मिला"
    assert record["items"][1]["raw_input"] == "बढ़ गए"
    assert record["follow_up_reasons"] == ["could_not_get_input", "crop_or_animal_damaged"]
    page.get_by_role("checkbox", name="English text", exact=True).uncheck()
    expect(page.locator("#card h2")).to_have_text("धन्यवाद")
    assert json.loads(page.locator("#record").inner_text())["answers"] == record["answers"]


def test_console_can_read_the_completion_in_english(demo_page):
    page, base = demo_page
    survey = json.loads((ROOT / "docs/voice/generated" / GENERATED).read_text())
    mount_console_player(page, base, survey)
    page.get_by_role("checkbox", name="English text", exact=True).check()
    page.get_by_role("checkbox", name="English audio", exact=True).check()
    page.get_by_role("radio", name="Yes", exact=True).check()
    page.get_by_role("button", name="Next", exact=True).click()
    page.get_by_role("radio", name="Fewer", exact=True).check()
    page.get_by_role("button", name="Finish", exact=True).click()
    page.get_by_role("button", name="Read aloud", exact=True).click()
    assert page.evaluate("window.spoken.at(-1).lang") == "en-IN"
    assert "Thank you" in page.evaluate("window.spoken.at(-1).text")


def test_console_follow_up_reaction_switches_language_with_the_answer(demo_page):
    page, base = demo_page
    survey = json.loads((ROOT / "docs/voice/generated" / GENERATED).read_text())
    mount_console_player(page, base, survey)
    page.get_by_role("checkbox", name="English text", exact=True).check()
    page.get_by_role("radio", name="Could not get the insecticide", exact=True).check()
    expect(page.locator(".player-hint")).to_contain_text("follow-up")
    page.get_by_role("checkbox", name="English text", exact=True).uncheck()
    expect(page.locator(".player-hint")).to_contain_text("आगे संपर्क")
    page.get_by_role("checkbox", name="English text", exact=True).check()
    page.get_by_role("button", name="Next", exact=True).click()
    page.get_by_role("radio", name="Fewer", exact=True).check()
    page.get_by_role("button", name="Finish", exact=True).click()
    expect(page.locator(".player-body")).to_contain_text("follow-up")
    page.get_by_role("checkbox", name="English audio", exact=True).check()
    page.get_by_role("button", name="Read aloud", exact=True).click()
    assert "follow-up" in page.evaluate("window.spoken.at(-1).text")
    cancelled = page.evaluate("window.cancelled")
    page.locator("#player").evaluate("node => node.remove()")
    page.wait_for_function("window.cancelled > " + str(cancelled))
