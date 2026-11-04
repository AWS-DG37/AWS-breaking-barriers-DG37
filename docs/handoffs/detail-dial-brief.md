# Brief: the detail dial for the MEL Console

Status: design brief, 4 November 2026. Written for the agent that implements it.
The design was picked from three options on the canvas
<https://claude.ai/artifact/EaLuseTsg4be4xJWhs1LPk> (board "Turn the dial").

## Goal

One control in the masthead sets how much the console shows. It has three
positions: **Essential**, **Detailed**, **Everything**. Every element in the console
carries a level. The dial hides elements above the chosen level. It also trims
the navigation. The choice persists in the browser.

The Run page today shows about 20 things at once, six of them JSON. A
business-side visitor from Digital Green must see the outcome first and the
evidence only when they ask for it.

## Where the code lives

| Concern | File |
| --- | --- |
| Masthead, nav links, section hosts | `src/mel/explorer/static/index.html` |
| Routing and the Compare section | `src/mel/explorer/static/app.mjs` (`route()`, `renderExperiment`, `renderInspector`, `renderComparison`) |
| Generate and Runs screens | `src/mel/explorer/static/generate.mjs` (`runHeader`, `resultsTable`, `showDetail`, `composer`, `batchBody`) |
| Review lists and improvements | `src/mel/explorer/static/review.mjs` |
| Configure | `src/mel/explorer/static/configure.mjs` (`details`, `editor`, `STEPS`) |
| Cost and cap panels | `src/mel/explorer/static/costs.mjs` (`budgetInput`, `capPanel`, `costSummary`) |
| Journey chips | `src/mel/explorer/static/journey.mjs` |
| Element helper | `src/mel/explorer/static/dom.mjs` (`el(tag, attrs, ...children)`) |
| Styles and tokens | `src/mel/explorer/static/style.css` |
| Tests | `src/mel/explorer/static/*.test.mjs`, run with `node --test` |

`el()` sets any attribute it is given, so a level tag is one attribute:
`el('div',{class:'gen-metrics','data-level':2},...)`.

## The mechanism

Use CSS gating. Do not re-render on a level change.

1. A new module `src/mel/explorer/static/detail.mjs` owns the level:
   - `LEVELS = ['Essential','Detailed','Everything']`, numbered 1 to 3.
   - `level()` reads `localStorage` key `mel.detail`. A missing, invalid or
     out-of-range value reads as 1. Wrap storage reads and writes in try/catch,
     as `auth.mjs` does. Storage failures never throw to the caller.
   - `setLevel(n)` clamps to 1..3, stores it, and applies it.
   - `apply(section)` sets `document.documentElement.dataset.detail` to the
     **effective level** for that section (see Floors below) and updates the
     dial's pressed state.
   - `mountDial(host)` renders the control into the masthead: one `role="group"`
     with `aria-label="Detail level"` and three `<button type="button">`
     elements with `aria-pressed`. Keep the 44 px minimum height from the base
     button style.
2. `style.css` hides tagged elements above the effective level:

   ```css
   html[data-detail="1"] [data-level="2"],
   html[data-detail="1"] [data-level="3"],
   html[data-detail="2"] [data-level="3"] { display:none; }
   ```

   Add the dial's own styles beside `.masthead-status`. Reuse the existing
   tokens (`--deep`, `--control`, `--surface`). Match the segmented control on
   the canvas board: a pill group, the active segment filled with `--deep`.
3. `index.html` gets the dial host inside `.masthead-status`, before the health
   text. The nav links get levels: Compare `data-level="2"`, Configure
   `data-level="3"`.
4. `app.mjs` `route()` calls `apply(tab)` after it resolves the tab, so a hash
   change updates the effective level.

### Floors

Each section has a floor. The effective level is the larger of the dial and the
floor:

| Section | Floor |
| --- | --- |
| Generate, Runs, Review | 1 |
| Compare | 2 |
| Configure | 3 |

This keeps a deep link usable. A visitor who opens `#configure` at Essential sees
all of Configure, and the dial shows Everything while they are there. The stored
preference does not change. A nav link for the current section is always
visible: add `.tabs a[aria-current="true"] { display:inline-flex; }` after the
gating rule, or give the rule a `:not([aria-current="true"])` guard.

### Never hide what blocks an action

These stay at level 1 whatever the dial says:

- Any `.notice`, `.notice.error`, `role="alert"` and `role="status"` message.
- The spend cap panel when the cap is unset, because an unset cap refuses
  console runs. `capPanel()` must render its unset state without a level tag,
  and its set state with `data-level="2"`.
- Required form fields on a form the visitor can submit from that level.
- The run progress bar while a run is running.

## The level map

Tag these. Everything not listed stays at level 1.

### Runs and Generate run page (`generate.mjs`)

| Level | Elements |
| --- | --- |
| 1 | Back link. Heading. A new one-sentence summary of the counts (see below). Result rows with insight id, crop and status badge. The selected insight: id, badge, phone preview, "Farmer asked", "Advice given", "Add to review list". |
| 2 | The meta line (started, configuration, tokens). `costSummary` and the set-state `capPanel`. The outcome metric filter buttons (`.gen-metrics`). The "What happened" column text in the results table. The journey section from `insightJourney`. |
| 3 | The "Time" column. "Full insight" and "Full outcome" disclosures. The "Delete run" button. |

The summary sentence is a pure function in `domain.mjs`, for example
`summarizeCounts({accepted:5,skipped:4,rejected:1,failed:0})` returning
"5 surveys are ready to send. The rules skipped 4 insights. The reviewer rejected
1 draft." Omit a clause whose count is zero. Say "No results yet." when every
count is zero. Singular and plural must be correct. Put it under the heading in
`runHeader`.

### Generate form (`composer`, `batchBody`)

| Level | Elements |
| --- | --- |
| 1 | Dataset, filters, configuration version, "Before you run" summary, Run button, budget input. |
| 2 | The set-state cap panel, "Estimate cost" and its result. |
| 3 | Nothing extra. |

### Compare (`app.mjs`)

| Level | Elements |
| --- | --- |
| 2 (floor) | Heading, version and insight pickers, outcome line, journey, question preview, question analysis, the comparison table without token columns, the reanalyze picker. |
| 3 | Stage evidence picker and "Full recorded event". "Original insight", "Final outcome", "Experiment inputs and configuration", "Run manifest", "Analysis reference version" and assessments disclosures. The raw survey-wide review block. Input and output token columns, as a `data-level` on each `th` and `td`. |

### Review (`review.mjs`)

| Level | Elements |
| --- | --- |
| 1 | Lists, comments, the comment form, the "Improve a configuration" form. |
| 2 | Improvement history and verdicts. |
| 3 | Per-round judgement tables and prompt diffs. |

### Configure (`configure.mjs`)

Floor 3, so no tags are needed. Do not add any.

## Tests

Write the tests first. The suite runs with
`node --test src/mel/explorer/static/*.test.mjs` and uses the small DOM fixture
in `navigation.test.mjs`. Copy that fixture pattern. Do not add jsdom.

- `detail.test.mjs`: default level is 1. A stored `"3"` reads as 3. `"7"`, `"0"`,
  `"x"` and a throwing `localStorage` read as 1. `setLevel(5)` stores 3.
  `apply('configure')` at dial 1 sets `data-detail` to `"3"` and leaves the
  stored value at 1. `apply('runs')` at dial 2 sets `"2"`. The dial's buttons
  carry `aria-pressed` matching the effective level.
- `domain.test.mjs`: `summarizeCounts` cases above, including singular forms and
  the all-zero case.
- `navigation.test.mjs`: `route()` applies the floor when the hash changes to
  `#configure` and back to `#runs`.
- Keep every existing test green.

Python tests and `ruff` do not change. Run them anyway before the PR:
`uv run --locked pytest -q` and `uv run ruff check src tests scripts`.

## Manual check

Start the recorded demo from `docs/explorer.md` ("Start the recorded demo with
Docker"), import the recorded run, then check each dial position on Runs,
Compare and Review, and at a 390 px wide viewport. Confirm that:

- Essential on a run page shows the sentence, the rows and the phone, and no JSON.
- Detailed adds the metrics, the cost line and the journey.
- Everything adds the Time column, the disclosures and Delete run.
- Opening `#configure` from Essential shows Configure in full and the dial reads
  Everything. Returning to `#runs` reads Essential again.
- The dial survives a reload.
- Nothing hidden by the dial is needed to start a run when the cap is unset.

## Docs

Add a short "Detail level" paragraph to `docs/explorer.md` under "The hosted
console": the three positions, the floors, and that the choice is per browser.

## Scope guard

- No backend, API, schema or migration change.
- No new dependency. No framework.
- No re-render on a level change. CSS does the hiding.
- Do not restyle anything else. Do not move sections.
- Do not hide notices, errors, alerts, the unset cap panel or required fields.
- Do not store the level on the server or per user.

## Done means

- The tests above pass, and the existing suites pass.
- The manual check list is confirmed in the PR body, with the viewport widths used.
- `docs/explorer.md` has the paragraph.
- The diff went through cross-review before the PR per
  `~/claude-config/docs/cross-review-policy.md`, with P3 findings fixed in the
  same PR. The reviewers are the two families that did not build it.
- The PR body names the canvas link above as the design source.

## Not in this brief

- The Program lead text and "Decisions waiting for you" list from the "Pick your
  seat" board. That is a separate feature.
- Collapsing the journey evidence into folds (the "Fold it away" board).
- A level badge on each element. The canvas shows them for the demo only. Do not
  ship them.
