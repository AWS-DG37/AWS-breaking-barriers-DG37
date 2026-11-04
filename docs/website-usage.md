# Website usage and live controls

The Explorer budgets website model requests. Batch and CLI work remain outside this budget. Recorded imports never consume it.

Live execution starts disabled. The host must use `mel-explorer --allow-live`, and an operator must save a positive cumulative USD budget and sourced prices before enabling requests. `--no-live` overrides `--allow-live`. Existing nginx authentication and private ingress remain required deployment controls.

Set `MEL_EXPLORER_ALLOWED_ORIGINS` to a JSON array of exact public origins, including scheme and port where applicable. The default allows only `http://localhost:8765` and `http://127.0.0.1:8765`. Proxy host headers do not grant permission. Every mutation requires JSON and `X-MEL-Intent: operator`. Cross-site fetches and origins outside this list are refused.

## Review exposure before launching

Open **Usage and live controls**. Supply each model ID, native API and AWS region separately. Supply the input and output USD rates per million tokens, their source and effective date, and a documented maximum billable input-token bound with its source and assumptions. Use rates that cover every applicable context tier and routing mode. The app stores operator provenance but does not certify it. No prices are prefilled.

In **New live experiment**, select the native models and choose **Review cost estimate**. The quote displays each request's reservation and a pessimistic total for the configured graph and output-retry limits. A run may stop before that total if the remaining budget cannot cover its next request. Skips and schema rejection can reduce actual calls.

Launches require the quote's policy version and terms hash, plus a caller-generated launch UUID. Repeating the same launch identity and terms returns the original experiment without starting another run. Changing the request, graph configuration or pricing requires a fresh quote. Every later request checks the accepted policy version again. Disabling live mode stops the next request; transmitted requests may finish.

## What the estimates mean

Before each native request, a short transaction locks the singleton policy row, checks cumulative exposure and reserves the documented maximum input tokens plus the enforced output-token limit. The provider call occurs after this transaction commits. Responses output limits include reasoning tokens. Website Mantle clients disable SDK retries. Website Converse clients receive an explicit botocore client with one total attempt. Pydantic output retries and graph revisions each make a new ledger entry. Streaming is refused.

A complete response settles once at the immutable rates captured for that request. Updating controls never reprices previous requests, resets prior spend or releases held amounts. Remaining budget subtracts settled estimates and the larger of each unresolved request's reservation or observed estimate. This avoids counting a known partial subtotal twice.

A provider failure, cancellation, missing token dimension, unsupported cached or audio usage, or an observed overrun stops further execution. Unknown requests keep their full reservation and block all further website model requests. An overrun records the larger observed estimate and also blocks requests. Pending requests survive restarts and consume their full reservation; the app cannot distinguish a crashed process from a request still running. Nothing expires automatically.

The pinned Converse adapter includes cache-read and cache-write tokens in its normalized input-token total while retaining the separate cache counters. The guard rejects nonzero cache or audio counters rather than price them at the two text rates. Known reported input and output subtotals remain visible, but their partial estimate is not a complete cost. A normalized zero dimension is conservatively unknown because the SDK can also use zero for absent usage.

These figures are configured-price estimates. They are not an AWS invoice, an AWS account-wide cap or a guarantee against incorrect operator rates or documented bounds. They exclude unsupported billing dimensions and non-model services such as hosting, storage, networking and taxes. The reservation guarantee depends on accurate rates and documented bounds.

There is no uncertainty-dismissal endpoint. Reconciliation is a manual, audited operator action requiring preserved provider request IDs, provider billing evidence and the existing ledger rows. Raising the budget or re-saving controls does not clear the block.

## Inspect saved usage

`GET /api/usage` aggregates every relevant stored run, independently of the Explorer's experiment display limit. Optional `run_id` and `experiment_id` filters provide selected-call and model, API, region, role and stage breakdowns. Metered website requests, recorded history and other saved work remain separate. Historical roles are not inferred. Incomplete totals are unknown, with known token and estimated-cost subtotals labelled separately. No run listing is returned or silently truncated.

`GET /api/live-policy` returns persisted controls, effective host permission, blockers and cumulative exposure. `POST /api/usage/quote` accepts an `ExperimentSpec` without invoking a model. `PUT /api/live-policy` requires the current `expected_version`; stale updates return 409. `POST /api/experiments` requires `X-MEL-Launch-Id`, `X-MEL-Policy-Version` and `X-MEL-Quote` from the reviewed proposal.

## Verify locally without AWS

Use the dedicated PostgreSQL test database. Never point these checks at a demo or production database.

```sh
MEL_TEST_DB_PORT=55438 uv run pytest
node --test src/mel/explorer/static/domain.test.mjs
MEL_TEST_DB_PORT=55438 uv run python tests/usage_harness.py --port 18877 --schema usage_browser_local
```

The browser harness uses a dedicated schema, real routes, PostgreSQL and Pydantic test models through the real metering wrapper. AWS client construction raises immediately. Its models are synthetic and its prices must be labelled test fixtures. It does not seed or enable live policy. Stop the server after verification. The schema belongs to the test operator and can be removed after preserving the desired evidence.
