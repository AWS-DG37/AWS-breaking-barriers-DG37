MEL Survey Agent: participant pack (version 1)

Open index.html first. It explains the task, the input, the output, the rules
and the worked examples, and links to every file below. It works offline.

What you build
  Read farmer insights, decide which ones get a follow-up survey, write one to
  three tap-answer questions in the farmer's language, set a send window, hand
  the survey to a scheduler, and simulate the farmer's answers, failures
  included. Real delivery on WhatsApp, Telegram, phone calls or notifications
  is out of scope. All data is synthetic.

data/
  farmers.csv                   300 practice farmers in Bihar
  insights.csv, insights.jsonl  6,999 practice insights, the agent's input.
                                quality_flag "junk" marks rows broken on purpose.
  golden_set.jsonl              150 insights with the expected survey decision.
                                Version 1. Digital Green may update it during the event.
  crop_calendar_bihar_rabi.csv  Crop stages and dates for the rabi season

spreadsheets/
  The same data, and the worked examples, as plain Excel files. Row 1 is the header.

json/
  schemas/    JSON Schemas for insight, survey, schedule_handoff, response,
              no_survey and bottom_sheet
  rules/      rules.json (survey or not, send window, channels, priority, cap)
              and outcome_codes.json (every answer outcome and what happens next)
  scenarios/  six worked examples: insight, survey in en-IN and hi-IN,
              scheduler hand-off, what each channel shows, one answer per channel
  outcomes/   183 simulated answer records by channel, question type and case,
              successes and failures

How to start
  1. Read index.html.
  2. Validate a few rows of data/insights.jsonl against json/schemas/insight.schema.json.
  3. Make your agent produce surveys that pass json/schemas/survey.schema.json.
  4. Compare your decisions with data/golden_set.jsonl.
  5. Simulate answers and write records that pass json/schemas/response.schema.json.
