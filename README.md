# Digital Green: FarmerChat Contextual MEL Survey Agent

AWS Breaking Barriers 2026 (DC) for Digital Green Foundation.

- `dg_agent/models.py`: Pydantic models for insight, survey (items, options), no_survey,
  schedule_handoff, response, bottom_sheet and farmer, plus `validate(name, payload)`,
  which checks payloads against the pack's JSON Schemas (the source of truth) with jsonschema.
- `dg_agent/checks.py`: rules from `json/rules/rules.json`: `decide()` (survey or not:
  junk, resolution_type, verification.needed, season end) and `check_survey()`
  (1-3 items, prompts <= 400 chars, labels <= 20 chars, 2-10 options, language matches
  the insight's `conversation_language`, send hours 07:00-19:00, after the action window,
  no PII, no new advice).
- `data/pack/`: Digital Green participant pack v1 (synthetic). **Not committed**: it is
  gitignored because the pack arrived as confidential. Get it from Aneel's email
  (secure-attach link + passcode) and unzip so `data/pack/index.html` exists.
  Start with `data/pack/index.html`.

## Tests

    uv sync
    uv run pytest -q

Tests skip if `data/pack/` is missing. They check that all 6,999 insights load (723 junk rows
get no survey), the 6 worked scenarios and 183 outcome records validate, and they print
golden-set accuracy for `decide()` (currently 96.7%, 145/150).

Known golden misses: R2 `critical_needs_human_call` (G-0016, G-0030, G-0078, G-0138) and
R7 `crop_already_harvested` (G-0136). The pack does not document these rules yet.
