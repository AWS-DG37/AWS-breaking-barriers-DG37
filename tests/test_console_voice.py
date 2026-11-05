"""The console's survey phone speaks with ElevenLabs through /api/voice, not the browser voice.

The demo writes a new survey on every run, so no recording can exist in advance: the
console makes each line when the phone asks for it, and keeps recent lines so a replay is
free. Each call spends ElevenLabs credits, so view-only users cannot make one.
"""

from fastapi.testclient import TestClient
from test_auth import ISSUER, SETTINGS, VIEWER_PASSWORD, Keys, token, viewer_settings
from test_explorer_api import Store

from mel.explorer.api import create_app
from mel.explorer.auth import SESSION_COOKIE, TokenVerifier

LINE = "Did you spray the field this week? Yes. Or No."
MP3 = b"\xff\xf3mp3 audio"


class Speaker:
    def __init__(self, fail=False):
        self.texts, self.fail = [], fail

    def __call__(self, text):
        self.texts.append(text)
        if self.fail:
            raise RuntimeError("ElevenLabs returned HTTP 401: invalid key")
        return MP3


def client(speaker=None, settings=SETTINGS):
    app = create_app(
        settings=settings,
        store_factory=lambda _: Store(),
        auth_verifier=TokenVerifier(ISSUER, "console-client", keys=Keys()),
        speaker=speaker,
    )
    return TestClient(app, base_url="https://console.example", follow_redirects=False)


def signed_in(http):
    http.cookies.set(SESSION_COOKIE, token())
    return http


def speak(http, text=LINE, language="en-IN"):
    return http.get("/api/voice", params={"language": language, "text": text})


def test_the_phone_gets_elevenlabs_audio_and_a_replay_is_free():
    speaker = Speaker()
    with client(speaker) as http:
        first = speak(signed_in(http))
        assert first.status_code == 200
        assert first.headers["content-type"] == "audio/mpeg"
        assert first.content == MP3
        assert speak(http).content == MP3
    assert speaker.texts == [LINE]


def test_the_same_words_in_another_language_are_a_different_line():
    speaker = Speaker()
    with client(speaker) as http:
        signed_in(http)
        speak(http, "FarmerChat", "hi-IN")
        speak(http, "FarmerChat", "en-IN")
    assert speaker.texts == ["FarmerChat", "FarmerChat"]


def test_hindi_text_is_spoken_as_given():
    speaker = Speaker()
    with client(speaker) as http:
        assert (
            speak(signed_in(http), "क्या आपने छिड़काव किया? हाँ। या नहीं।", "hi-IN").status_code == 200
        )
    assert speaker.texts == ["क्या आपने छिड़काव किया? हाँ। या नहीं।"]


def test_only_survey_sized_lines_in_survey_languages_are_spoken():
    speaker = Speaker()
    with client(speaker) as http:
        signed_in(http)
        assert speak(http, "x" * 601).status_code == 422
        assert speak(http, "   ").status_code == 422
        assert speak(http, LINE, "fr-FR").status_code == 422
    assert speaker.texts == []


def test_without_a_key_the_phone_is_told_voice_is_off():
    with client() as http:
        response = speak(signed_in(http))
    assert response.status_code == 503
    assert "not configured" in response.json()["detail"]


def test_a_failed_recording_is_an_error_without_the_provider_detail():
    with client(Speaker(fail=True)) as http:
        response = speak(signed_in(http))
    assert response.status_code == 502
    assert "invalid key" not in response.text


def test_a_failed_line_is_not_remembered():
    speaker = Speaker(fail=True)
    with client(speaker) as http:
        signed_in(http)
        speak(http)
        speaker.fail = False
        assert speak(http).content == MP3
    assert speaker.texts == [LINE, LINE]


def test_voice_needs_a_session_and_viewers_cannot_spend_credits():
    speaker = Speaker()
    with client(speaker, viewer_settings()) as http:
        assert speak(http).status_code == 401
        assert http.post("/api/auth/viewer", json={"password": VIEWER_PASSWORD}).status_code == 200
        response = speak(http)
        assert response.status_code == 403
        assert "view-only" in response.json()["detail"].lower()
    assert speaker.texts == []
