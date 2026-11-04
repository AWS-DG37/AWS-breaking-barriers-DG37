from pathlib import Path

from streamlit.testing.v1 import AppTest

from dg_agent import bedrock


APP = Path(__file__).resolve().parents[1] / "bedrock_app.py"


def test_chat_displays_a_response_and_can_start_over(monkeypatch):
    def answer(messages):
        return {"text": "Hello from Bedrock.", "input_tokens": 8, "output_tokens": 4}

    monkeypatch.setattr(bedrock, "chat", answer)
    app = AppTest.from_file(str(APP)).run()
    assert not app.exception
    app.chat_input[0].set_value("Hello").run()
    assert [message.name for message in app.chat_message] == ["user", "assistant"]
    assert "Hello from Bedrock." in [element.value for element in app.markdown]
    assert "8 input" in " ".join(element.value for element in app.caption)
    app.button(key="new_chat").click().run()
    assert not app.chat_message
    assert not app.exception


def test_failed_prompt_can_be_retried_without_duplicating_history(monkeypatch):
    def unavailable(messages):
        raise bedrock.BedrockError("Bedrock is busy. Please try again.")

    monkeypatch.setattr(bedrock, "chat", unavailable)
    app = AppTest.from_file(str(APP)).run()
    app.chat_input[0].set_value("Hello").run()
    assert "try again" in app.error[0].value
    assert app.session_state["messages"] == []

    def answer(messages):
        assert messages == [{"role": "user", "content": "Hello"}]
        return {"text": "Recovered.", "input_tokens": 8, "output_tokens": 2}

    monkeypatch.setattr(bedrock, "chat", answer)
    app.button(key="retry").click().run()
    assert len(app.chat_message) == 2
    assert not app.error
    assert not app.exception
