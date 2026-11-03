"""Run with: uv run streamlit run bedrock_app.py"""

import streamlit as st

from dg_agent.bedrock import BedrockError, MODEL_ID, PROFILE, REGION, chat


st.set_page_config(page_title="Field Echo · Bedrock chat", layout="centered")
st.html("""<style>
  ::selection {background: #E8B45A; color: #174B38;}
  :focus-visible {outline: 3px solid #805A1A !important; outline-offset: 3px;}
  textarea {caret-color: #174B38;}
  [data-testid="stCaptionContainer"] {opacity: 1 !important;}
  [data-testid="stCaptionContainer"], [data-testid="stCaptionContainer"] p,
  textarea::placeholder {color: #4E6254 !important; opacity: 1 !important;}
</style>""")

for key, default in (("messages", []), ("failed_prompt", None), ("error", None), ("usage", None)):
    if key not in st.session_state:
        st.session_state[key] = default

heading, reset = st.columns([4, 1], vertical_alignment="center")
with heading:
    st.title("Chat with Bedrock")
with reset:
    if st.button("New chat", key="new_chat", width="stretch",
                 disabled=not st.session_state.messages and not st.session_state.failed_prompt):
        st.session_state.messages = []
        st.session_state.failed_prompt = None
        st.session_state.error = None
        st.session_state.usage = None
        st.rerun()

st.caption("Field Echo · Powered by Amazon Nova Lite")
st.write("Ask a question, explore an idea, or try a prompt. Follow up to keep the conversation going.")

with st.expander("Connection details"):
    st.text(f"Model: {MODEL_ID}\nRegion: {REGION}\nAWS profile: {PROFILE}")
    st.caption("Prompts go to Amazon Bedrock. AWS credentials stay on this Mac. "
               "Chat history stays in this browser session and is cleared by New chat.")

if not st.session_state.messages and not st.session_state.failed_prompt:
    st.subheader("What would you like to try?")
    st.markdown("Try **‘Explain Bedrock in three sentences’** or **‘Help me outline a project demo.’**")

for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])

retry = False
if st.session_state.failed_prompt:
    with st.chat_message("user"):
        st.markdown(st.session_state.failed_prompt)
    st.error(st.session_state.error)
    retry = st.button("Try again", key="retry")

if st.session_state.usage:
    usage = st.session_state.usage
    st.caption(f"Last reply · {usage['input_tokens']:,} input tokens · {usage['output_tokens']:,} output tokens")

at_limit = len(st.session_state.messages) >= 20
if at_limit:
    st.info("You've reached ten exchanges. Select New chat to start fresh.")
prompt = st.chat_input("Ask a question…", max_chars=4000, disabled=at_limit)
if retry:
    prompt = st.session_state.failed_prompt

if prompt:
    if not retry:
        with st.chat_message("user"):
            st.markdown(prompt)
    with st.spinner("Asking Nova Lite…"):
        try:
            result = chat(st.session_state.messages + [{"role": "user", "content": prompt}])
        except (BedrockError, ValueError) as error:
            st.session_state.failed_prompt = prompt
            st.session_state.error = str(error)
        else:
            st.session_state.messages.extend([
                {"role": "user", "content": prompt},
                {"role": "assistant", "content": result["text"]},
            ])
            st.session_state.usage = result
            st.session_state.failed_prompt = None
            st.session_state.error = None
    st.rerun()
