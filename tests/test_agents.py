"""Agent construction choices that depend on the model API."""

from mel.agents import converse_profile


def test_converse_never_forces_the_output_tool_but_keeps_the_model_profile():
    profile = converse_profile("us.anthropic.claude-sonnet-5-5")
    assert profile.bedrock_supports_tool_choice is False
    assert profile.bedrock_thinking_variant == "anthropic"
    assert converse_profile("us.amazon.nova-pro-v1:0").bedrock_supports_tool_choice is False
