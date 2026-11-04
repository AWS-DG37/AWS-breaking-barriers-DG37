# Field Echo delivery to the existing EC2 host

A push to `main` runs credentialless application checks, real PostgreSQL 16 tests,
Node tests and a Chromium smoke of an amd64 image built from the complete checkout.
Only that event can obtain AWS credentials and publish an immutable exact-SHA tag.
The release command resolves its digest, checks that the SHA still heads `main`,
and dispatches the fixed, version-pinned SSM document. Pull requests get checks
without deployment credentials. There is no manual feature-ref trigger.

The committed [`deploy/target.json`](../deploy/target.json) is the nonsecret delivery
target. Current infrastructure remains account `047640882913`, region `us-east-1`,
EC2 `i-01c706a91df84e556`, host role `dg37-explorer-workshop-host`, compose project
`dg37-explorer-cloud`, `/opt/dg37`, and CloudFront `E3H038T8P3IIWL` at
`https://fieldecho.org`. The workflow does not create DNS, CloudFront, SAM, databases,
networking, Cognito users or inference permissions.

## Bootstrap and operator interface

Review and apply `deploy/bootstrap.yaml` once with an authenticated workshop operator.
It creates only the delivery ECR repository, main OIDC role, constrained SSM document,
and an ECR pull policy on the existing host role. Set `CreateOidcProvider=true` only
when the account has no GitHub provider. The role trusts audience `sts.amazonaws.com`
and the exact immutable subject
`repo:AWS-DG37@352139567/AWS-breaking-barriers-DG37@1444724104:ref:refs/heads/main`.
Do not add a GitHub environment without reviewing the resulting OIDC subject.

```sh
aws cloudformation deploy --region us-east-1 --stack-name dg37-main-cd \
  --template-file deploy/bootstrap.yaml --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides CreateOidcProvider=true
python deploy/fieldecho.py status --target deploy/target.json
```

The public Python calls are `deploy(target, full_sha, workflow_sequence)` and
`status(target)`. Both use `DG37-DeployMain-v1`, version `1`; CI cannot call
`AWS-RunShellScript`. The SSM document accepts only deploy/status, a full SHA,
immutable digest and positive sequence. Its shell is fixed and uses validated
SSM environment parameters, never caller-supplied shell. It pulls the image,
checks its revision label, extracts this image's handler and target, and runs the
handler under host Python 3.9. Status uses the last successfully activated artifact
through one atomically replaced `delivery/current` directory symlink. Rejected and
failed releases cannot replace the status handler or its target;
before the first deployment, no managed status exists and that command fails loud.
A document-interface change requires a new versioned document name and reviewed
target update; silently editing deployed document version 1 is not an update path.

The CI role can publish only this repository, send only this document to this
instance, and read command results. ECR authorization tokens and SSM result reads
require wildcard IAM resources; publish and SendCommand resources are bounded.
The host gains pull access only. There is no new Bedrock grant. A principal that
can publish and deliver the handler effectively has root on this host, so the
exact main-subject trust is the authorization boundary. The host has no GitHub
credentials and does not independently prove a workflow run's identity.

## Preflight and preserved infrastructure

Before enabling this workflow, verify the SSM agent is online and supports parameter
`ENV_VAR` interpolation, Docker Compose supports `config --no-env-resolution
--format json`, ECR is reachable, and the database role can create the isolated
rehearsal database. Confirm enough room for a full SQL snapshot and restored copy.
The candidate must read the configured participant dataset as UID 10001 through
an existing mount. Images deliberately omit private insight datasets. Missing
`MEL_PACK_URI`/dataset availability fails before stopping production; provision the
approved existing data mount explicitly before initial adoption. No S3 stack is
created as a fallback. The initial host's existing dataset is at
`/opt/dg37/app/docs/participant-pack/data/insights.jsonl`, with a second copy in its
old release tree. Before first CI adoption, an operator must preserve the source,
copy the approved dataset into a stable host data directory, mount it read-only
for explorer, and set `MEL_PACK_URI` to that container path in `explorer.env`.
Review and verify that one-time setup separately; ordinary releases retain it.

The handler never edits `explorer.env`, `database.env`, passwords, nginx, Basic auth,
project identity, database service or existing volumes. It renders Compose as JSON
(valid YAML) through the installed Docker Compose parser, keeping env-file
references unexpanded. Only the explorer image, offline entrypoint and obsolete
build/healthcheck settings change. It refuses extra compose services or any
`mel-worker` container, including a stopped one. Operators must remove a worker
container before delivery. The committed boot compose has only explorer/postgres;
the explorer always starts explicitly with `--no-live`, including rollback.

The existing explorer must already use host networking, without attached networks
or published ports. Its effective `MEL_*` settings, resolved by Docker Compose, must
match `explorer.env` alone. Identical inline host and port values are accepted.
The database host must be loopback. A bridge host such as `postgres`, or a Compose
override that would send the migration probes to a different database, fails before
any explorer replacement. Convert incompatible hosts in a separate reviewed operator
change. Delivery does not rewrite their network or database configuration.

## Activation and recovery

Workflow-level serialization has cancellation disabled. The CLI checks current
main immediately before SSM dispatch. A host flock and atomically written
`delivery-state.json` bind the highest admitted workflow sequence to its exact SHA
and image digest, including failed attempts. An exact active replay verifies serving
readiness and returns `ALREADY_DEPLOYED`; an exact failed or recovered release can
retry. Lower sequences and same-sequence identity changes are refused.
The workflow path is persisted: renaming the workflow requires an explicit operator
state migration rather than quietly resetting the sequence. Replaying an older run
cannot replace a newer release, even when that newer release rolled back.

On initial adoption the handler retains the running local image ID and gives it a
rollback tag. It never prunes local images, including the old `45eb13b` image absent
from ECR. A release record contains SHA, image identity and workflow sequence; host
state owns deployed/previous, highest admitted sequence and release identity, outcome
and transition.

1. Validate the artifact, data mount, and current host contract. Render both the
   previous and candidate Compose files and validate each with the installed
   `docker compose config --quiet` parser. Preserve the exact original Compose as
   root-readable `compose.original.yaml`, without overwriting an existing copy.
   Persist the admitted sequence and explicit transition before any replacement.
   Start the previous image from the rendered healthy Compose and prove readiness
   before taking a snapshot or migrating. Only then persist healthy boot Compose.
2. Stop the explorer; leave PostgreSQL running. Capture the migration ledger,
   original column names, counts and hashes of all existing public-table rows,
   and a root-readable SQL dump under a unique `backups/<sequence>-<sha>/attempt-*/` directory. Earlier dumps are retained.
3. Restore a separate rehearsal database. Apply the candidate's packaged migrations
   twice using `Store.initialize`. Preserve every existing filename/checksum ledger
   row and every original-column row fingerprint. The old
   `003_website_usage.sql` coexists with `003_configs_datasets_jobs.sql`.
4. Run the previous image against the migrated copy, read recorded experiments, and
   start its actual console offline on loopback port 8878. Prove database access,
   static resources, HTTP authentication and offline process arguments.
5. Explicitly call the candidate's `Store.initialize` against production, then verify
   the same preservation invariants before switching the application.
6. Switch only the explorer, prove its actual image ID, DB connection, static page
   and asset, unauthenticated health behavior, explicit offline arguments and worker
   absence. Auth requires 401; without auth, health requires HTTP 200 with
   `database == "connected"`. A 200 with an unavailable DB fails.
7. Commit healthy compose and state. On an ordinary failure restore the prior image
   and healthy compose. On interruption, recover that saved compose before admitting
   any new request. Recovery cleans only containers carrying that sequence's delivery
   label and its named rehearsal database, preserving the highest sequence.

Compose and state are two files, not an atomic pair. The transition remains until
both activation and compose persistence succeed. A crash before the container
switch, after the switch, or between compose and state commits therefore causes
recovery to start the saved healthy release. Recovery failure preserves the intent
and stops further admission. On reboot, the existing systemd unit starts the last
committed compose; the next deploy performs recovery before any new activation.

The SSM document establishes one deadline before lock acquisition, image pull, and
handler extraction. Startup commands are bounded by the same activation cutoff.
Activation has at most 1320 seconds from document start. A further 360 seconds is
reserved for recovery, with a final cutoff at 1680 seconds, below SSM's 1800-second
hard timeout. Each host subprocess is clamped to the remaining phase budget.
An activation alarm, SIGTERM, or SIGINT becomes a recoverable exception. The same
invocation restores the prior image and Compose without waiting for a new push.
Recovery never extends the cutoff. If recovery itself fails or exhausts its budget,
the transition and sequence fence remain for operator attention or the next attempt.
SIGKILL, host loss, and a Docker daemon that cannot execute recovery still need that
persisted-intent recovery path. These limits do not make those failures recoverable
inside a killed process.

Full SQL dumps are retained for every attempt. Delivery never removes database
backups. Operators must monitor free space and choose an archive and retention
limit before backup growth exhausts the host. Automatic retention remains a separate
reviewed follow-up.

`Store.initialize` already puts all packaged migration execution and ledger changes
inside one PostgreSQL transaction with an advisory lock. Applied migration bytes
are never rewritten. This mechanism does not automatically restore the production
SQL dump: an image rollback retains migrated schema and accepted data. A manual
restore could discard later writes and is a separate operator decision. A bad
successfully deployed release is undone with a revert on `main`, through this path.

## Authentication and account lifetime

Provision the five authorized website administrators through the separate Cognito
operator module, outside CI and this bootstrap stack. Keep account identities,
passwords and the private input file out of source. The four runtime auth variables
are retained unchanged by delivery. All signed-in users have current main's full
console permissions; no new RBAC framework is added here.

Initial main adoption must happen under the existing Basic auth with Cognito
variables unset, so the old image remains a valid protected fallback. Then configure
Cognito separately, prove a real sign-in, and only then consider removing Basic
auth. Subsequent releases require the previous image to pass strict auth readiness
on the migrated copy. No test token or auth bypass is introduced.

An expired workshop account loses its provider, roles and ECR. Recovery is an
operator bootstrap in the replacement account plus a reviewed `target.json` update.
Deliveries fail loudly while that is pending. ECR and the SSM document have retention
policies; deleting a stack does not silently remove rollback artifacts. No image
lifecycle deletion is installed, so an operator must review future storage cleanup.

## Design choice and verification limits

This implements the synthesized Claude-base design: an image-carried, Python 3.9
stdlib handler and one committed target. It grafts the Codex fixed SSM interface and
current-main freshness check. It corrects the sketches' compose/state crash window
with explicit transition intent, and replaces health-only readiness with direct DB,
static, auth-negative and actual serving-image proof.

Host builds lost because they add production CPU/dependency-network contention and
cannot smoke the exact artifact before activation. A production self-hosted runner,
SAM/RDS migration and a background pull agent add trust or infrastructure outside
this job. The accepted tradeoff is a brief single-host pause and whole-database
rehearsal for each release; its practical ceiling is workshop-sized databases.

The workflow uses locked Python 3.12 dependencies, pinned verified official action
commits, full pytest with real PostgreSQL, Node and Chromium checks. It blocks the
browser's AWS model catalog and external HTTPS; no paid script or inference runs.
The host entry point is also executed with isolated Python 3.9. Application/deployment
Ruff checks cover `src` and `deploy`: the baseline has 53 legacy generator/test
lint errors and 20 `dg_agent` lint errors, deliberately outside this new lint gate.
Focused delivery tests are linted locally; all tests remain in the functional gate.

Local unit checks replace only Docker/AWS subprocess boundaries. They prove
admission, journal recovery windows, rollback, malformed inputs and health semantics;
they do not prove the actual EC2 rollout or real-data migration. Live rehearsal,
Cognito sign-in, host recovery and deployed identity need independent target evidence
before claiming end-to-end delivery.
