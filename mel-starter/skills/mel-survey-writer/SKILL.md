---
name: mel-survey-writer
description: Write and check farmer follow-up survey questions for the MEL Survey Agent (FarmerChat, Digital Green, AWS hackathon). Use this skill whenever the user wants to generate survey questions or answer choices from an insight, draft or tune the generator or verifier agent prompts, write Hindi or English tap-answer surveys for farmers, validate a survey against survey.schema.json, check follow_up reasons, label length or voice-safety, or build the verify-and-retry loop, even if they only say "write the questions" or "check this survey".
---

# MEL survey writer

This skill writes the questions for one insight and checks them. A model writes the wording, code enforces every rule that code can enforce, and a second model judges meaning.

## Workflow

1. Confirm the insight passed the gate (use the mel-survey-rules skill). Never write a survey for an insight that got no survey.
2. Build the generator prompt from references/generator_prompt.md. Fill the user template from the insight. Add one or two matching examples from references/examples (the brief's own worked surveys, in hi-IN and en-IN).
3. Parse the model's JSON, then run `python scripts/check_survey.py generated.json insight.json`. Any ERROR means regenerate. WARN lines are voice or style risks.
4. If code passes, run the verifier from references/verifier_prompt.md on the insight and items.
5. Verdict pass: add survey_id, farmer_id, source_insight_ids, language and send_window in code, then hand off. Verdict revise: send the issues list back with the retry addendum, at most 2 retries. Verdict fail, or retries used up: use a fixed template for that resolution type.

## How the questions are decided

- The insight sets the topic. Questions only ask about the advice in advice_summary and what happened after it, using plain words such as "the spray for the green insects".
- resolution_type sets the order: first whether the farmer did it (or watched, or held off, or started the plan, or saw the expert), then how it went.
- Rules set the shape: 1 to 3 items (prefer 2), confirm has exactly yes and no, single-select has 2 to 4 answers, scale is 1 to 5, labels are at most 20 characters, no brackets or symbols, no exact dates.
- Bad outcomes carry a follow_up from a fixed list of four: advice_did_not_work, crop_or_animal_damaged, could_not_get_input, could_not_reach_expert. Scales use follow_up_at_or_below plus follow_up_reason.
- Surveys are voice-safe by default so one survey works on every channel. The 4-option cap is a design choice, not checked against the voice renderer.
- Never new advice, never personal information, one language matching conversation_language.

## Cost note

Advice text is highly repeated (176 distinct advice strings across about 2,777 eligible insights). Cache the generated skeleton by advice, resolution type and language, then fill crop and symptom words in code. State this trade-off openly when presenting.

## What the checker covers

check_survey.py checks the schema, language match, item ids, option counts per type, label length in Unicode characters, follow_up values, emoji, exact dates, a basic personal-information word list, script mix, and whether any bad outcome is routable. It passed all 10 of the brief's worked surveys with no errors (two bracket warnings) and caught 8 of 8 injected defects. It does not judge meaning. That is the verifier's job, and the keyword list is a first pass, not proof.

## Limits

The golden set only has plain English questions, so use it to compare question count, type and topic, not wording. do_not_act has no worked example, so write one by hand before relying on few-shot for it.
