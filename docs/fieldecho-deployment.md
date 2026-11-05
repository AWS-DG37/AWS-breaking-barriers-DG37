# Field Echo delivery to the existing EC2 host

A push or `workflow_dispatch` on `main` runs credentialless application checks,
real PostgreSQL 16 tests, Node tests and a Chromium smoke of an amd64 image built
from the complete checkout. Only those events on `refs/heads/main` can obtain AWS
credentials and publish an immutable exact-SHA tag.
The release command resolves its digest, checks that the SHA still heads `main`,
and dispatches the fixed, version-pinned SSM document. Pull requests get checks
without deployment credentials. Feature-ref dispatches get checks without image
upload or deployment credentials. Main pushes and dispatches share one concurrency group.

The voice clip workflow pushes generated clips with `GITHUB_TOKEN`, which does not
start a push-triggered delivery run. After its commit step succeeds, it requests
`gh workflow run main.yml --ref main` when the checked-out HEAD differs from the
original voice run's SHA. This also retries a failed dispatch when a rerun finds
clips already published. An unchanged initial run does not request another delivery.

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
references unexpanded. Only the explorer image, execution entrypoint, stop grace
period and obsolete
build/healthcheck settings change. It refuses extra compose services, `mel-worker`
containers (including stopped ones), stray integrated runners, and native queue
workers. Operators resolve these inventories before delivery. The boot Compose
has only explorer/postgres. Missing host policy means explicit `--no-live`;
normal candidates honor retained live policy. Previous-image checks, rehearsal and
recovery always use explicit offline arguments supported by the previous image.

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
readiness and execution mode before
returning `ALREADY_DEPLOYED`; a mode mismatch is reconciled through the transition
journal. An exact failed or recovered release can retry. Lower sequences and
same-sequence identity changes are refused.
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
   Normalize the current boot file separately, pin its explorer to the previous
   image, remove live-mode flags, and add exactly one `--no-live`. The boot command
   must explicitly invoke `mel-explorer` with argument lists, without a shell.
   Validate this offline boot snapshot with Compose before persisting it in the
   transition alongside the admitted candidate and rendered healthy Compose.
   Start the previous image from the rendered healthy Compose and prove readiness
   before taking a snapshot or migrating. Only then update the persisted boot
   snapshot and boot file to that proven healthy Compose.
2. Stop and wait for the explorer; leave PostgreSQL running. Verify Docker reports
   it stopped, no scoped native/container runner remains, and no hosted queue lease
   remains in this database, waiting up to five seconds for backend disconnect.
   Perform the same stop proof before every replacement,
   including stopping a candidate before rollback. Capture the migration ledger,
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
   and asset, unauthenticated health behavior, expected execution arguments and stray
   worker absence. Live mode also requires complete Cognito settings and the advisory
   lease owned by this exact container's PID 1 connection. Auth requires 401;
   without auth, offline health requires HTTP 200 with
   `database == "connected"`. A 200 with an unavailable DB fails.
7. Commit healthy compose and state. On an ordinary failure restore the prior image
   and healthy compose. On interruption, recover that saved compose before admitting
   any new request. Recovery cleans only containers carrying that sequence's delivery
   label and its named rehearsal database, preserving the highest sequence.

Compose and state are two files, not an atomic pair. The transition remains until
both activation and compose persistence succeed. A crash before the container
switch, after the switch, or between compose and state commits therefore causes
recovery to start the saved healthy release. Recovery leaves the boot file unchanged
until readiness passes. If the rendered recovery fails, it attempts the persisted
offline boot snapshot once, verifies the previous image and readiness, and only then
commits that boot file and clears the transition. It never selects the current disk
file, which may already name the candidate, or the initial `compose.original.yaml`
as an automatic fallback. The original archive may contain `--allow-live`.
Legacy pending transitions without a boot snapshot can retry only their saved
healthy Compose. An unsuccessful recovery retains the transition and stops admission.
A cleanup failure also retains the transition even if the offline fallback starts.
On reboot, the existing systemd unit starts the last committed Compose. The next
deploy performs recovery before any new activation.

The SSM document establishes one deadline before lock acquisition, image pull, and
handler extraction. Startup commands are bounded by the same activation cutoff.
Activation has at most 1320 seconds from document start. A further 360 seconds is
reserved for recovery, with a final cutoff at 1680 seconds, below SSM's 1800-second
hard timeout. Each host subprocess is clamped to the remaining phase budget.
An activation alarm, SIGTERM, or SIGINT becomes a recoverable exception. The same
invocation restores the prior image and Compose without waiting for a new push.
When a boot snapshot is available, the first recovery attempt, including cleanup,
gets half the remaining recovery budget. The single offline fallback can use the
rest, within the same original cutoff. Recovery never extends that cutoff or starts
a fallback after it expires. If recovery itself fails or exhausts its budget, the
transition and sequence fence remain for operator attention or the next attempt.
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

## Hosted queue opt-in and shutdown

The host file `/opt/dg37/live-policy.json` contains one JSON boolean. A missing file
means `false`. Malformed values, symlinks, a different owner, or group/world writable
files refuse activation. The host handler runs as root and atomically writes this
file under the existing delivery flock. It is outside the image, release tree and
`delivery-state.json`; older handlers continue to read the existing state schema.
Do not hand-edit the policy or Compose to enable execution.

Install a healthy image containing `--run-jobs` while offline first. On the EC2 host,
use the successfully activated handler and target under `/opt/dg37/delivery/current`.
These are host-local operator commands, outside the fixed CI SSM deploy/status API.

```sh
sudo python3 /opt/dg37/delivery/current/handler.py host-mode --mode live \
  --target /opt/dg37/delivery/current/target.json
sudo python3 /opt/dg37/delivery/current/handler.py host-status \
  --target /opt/dg37/delivery/current/target.json
sudo python3 /opt/dg37/delivery/current/handler.py host-mode --mode offline \
  --target /opt/dg37/delivery/current/target.json
```

Live opt-in validates complete Cognito authentication, absence of ECS dispatch,
instance-role credentials obtained through IMDS, the expected account and existing
workshop host role, and the current cap record. These checks make no inference call,
add no IAM grant, and never alter the cap. An unset cap stays unset and existing cost
guards refuse paid console jobs until an operator deliberately sets it.

Initial opt-in brings the previous image up offline, checks that both queued and
running job inventories are empty, and refuses any unexpected backlog without
changing its rows or the policy. It uses the same snapshot, migration rehearsal,
readiness and transition journal as a release, even for the same image. Host mode
changes retain the highest admitted release identity and workflow sequence fence.
An already authorized live restart may execute queued jobs. Running, failed and
completed-with-errors rows are never automatically resumed or rewritten.

Normal later CI candidates honor the retained boolean. Recovery always restores the
previous image offline and preserves the desired policy. Status therefore reports
`desired_mode`, `applied_mode` and `ready` separately. A desired-live/applied-offline
result is a hold after recovery, not successful live activation. After checking the
failure and outstanding rows, deliberately reconcile with `host-mode --mode live`,
or select offline to revoke the policy. A subsequent successful release also
reconciles retained live policy. Reboot starts the last committed Compose.

`mel-explorer --run-jobs` starts one queue task in the authenticated API process with
its own Store connection. Startup waits for initialization and a nonblocking exclusive
session advisory lease. Its `application_name` includes this container hostname and
PID; a lock held by another container cannot satisfy readiness. The task's return,
exception or connection failure closes job admission and stops the actual Uvicorn
server with a nonzero process result. Docker's existing restart policy restarts the
whole application. There is no worker reconnect loop or automatic resume. Readiness
proves initialization and owned connection, not provider execution or uninterrupted
progress during a long model request.

SIGTERM closes admission and requests cooperative stop before another claim. An
in-flight threaded claim is awaited before Store closure; a committed claim observed
after stop remains running without starting its provider. Existing active work gets
a 25-second drain after the HTTP shutdown phase (bounded to five seconds), followed
by one cancellation and awaited unwind. Existing cost accounting retains unknown
request reservations and checkpoints. Docker stop uses a 40-second outer termination
bound and Compose retains a 45-second stop grace period. A forced outer termination
can leave running rows and unresolved reservations for explicit operator review.

New offline normalization strips `--run-jobs`. If the older 426 handler retains that
flag when normalizing a new image, the new CLI accepts both flags and gives
`--no-live` precedence, so it starts no runner. No new flag is passed to an old image.
The policy file adds no dependency to old-image rollback.

## AgentCore code releases

The same workflow checks an AgentCore CodeZip on a credentialless Linux ARM64
Python 3.12 runner. `agents/requirements.lock` pins every transitive dependency and
its allowed hashes; wheel-only installation rejects a source build. The package
contains the runtime Python modules and skill prompts, schemas and reference
examples. Workshop datasets, caches and local runtime state are excluded. The
unpacked package imports the real entry point and exercises options and invalid
rules input without model calls. Its embedded `release.json` records the tested
commit. Only a main push or main dispatch uploads the tested artifact for release.

`deploy/agentcore-target.json` records the existing runtime, execution role and
versioned package bucket in account `047640882913`, `us-east-1`. No toolkit state or
inherited account defaults are used. An operator applies
`deploy/agentcore-policy.yaml` to the existing `dg37-main-deploy` role with those
exact target values. The additive policy permits only that runtime's Get/Update,
DEFAULT endpoint read and invocation, pass-role to its existing execution role,
and package read/write under the declared S3 prefix. CI cannot create IAM roles or
runtimes. The runtime execution role separately needs read access to that prefix.

Before mutation, `deploy/agentcore.py` validates caller account, runtime identity,
execution role, package revision and bucket versioning. Packages use
`<prefix>/<commit>/<sha256>.zip`, conditional creation, SHA-256 checksums and a
required non-null S3 version ID; retrying an existing package verifies its length,
metadata and checksum before reusing that version. The update preserves the
runtime's existing network, environment, protocol and other configuration. A
fresh remote-main check immediately precedes the update. Superseded runs perform
no runtime update. A same-package rerun verifies the running endpoint and returns
`ALREADY_DEPLOYED` without another upload or runtime version. A runtime in
`UPDATE_FAILED` is eligible for a fix-forward update after its underlying problem
has been corrected, including when the tested package is unchanged. Other failed
states and in-progress transitions are refused. The [UpdateAgentRuntime API](https://docs.aws.amazon.com/bedrock-agentcore-control/latest/APIReference/API_UpdateAgentRuntime.html)
documents no READY-only prerequisite; the service remains the authority and any
rejection exits nonzero. Failed-state recovery is covered at the client boundary,
without deliberately breaking a live runtime to exercise the service.

The release job allows 60 minutes for the aggregate agent, endpoint and console
polling windows plus command overhead. Release logs record the prior runtime
version before an update. The new version must become READY, match the exact versioned package, and be served by the READY
DEFAULT endpoint. A new-session `options` invocation must return a successful
application response. Any failed state, timeout or smoke exits nonzero and blocks
console delivery. Prior runtime versions and immutable packages are retained for
operator recovery; this does not claim an automatic rollback of DEFAULT after a
failed smoke. AWS automatically advances DEFAULT to the new version on update
([runtime versioning](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/agent-runtime-versioning.html)).

After this gate, the existing console release resolves its tested image digest,
checks current main again and uses the existing fixed SSM document. Ordinary agent
updates retain the runtime ARN, so the operator-owned `/opt/dg37/explorer.env`
requires no change. Runtime provisioning, first-time environment configuration and
behavior verification remain operator-owned actions.

To refresh locked dependencies deliberately, run:

```sh
uv pip compile agents/requirements.txt --generate-hashes --python-version 3.12 \
  --python-platform aarch64-manylinux2014 -o agents/requirements.lock
```

Review the dependency changes and let both workflow check jobs pass before main
release. The console image and CodeZip must always come from that same tested SHA.
