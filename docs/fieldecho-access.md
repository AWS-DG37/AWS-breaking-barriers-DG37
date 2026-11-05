# Field Echo website administrators

Every authenticated Cognito pool member has full console access. The optional shared
viewer login described below grants read-only access without creating a pool member. The
`admins` group is an operator inventory label, not an application-enforced role and
not AWS IAM administration. Self-registration is disabled. Admit only the five
explicitly authorized website operators. This setup is independent of CI and image
releases; never store account lists or invitations in GitHub Actions.

## Bootstrap once, reuse thereafter

Use the workshop's already authenticated AWS operator session. Never supply AWS keys
or passwords to this script. Confirm the account and region privately with
`aws sts get-caller-identity` before changes. The commands below are operator instructions;
preparing this change does not execute them.

First inspect the existing CloudFormation stacks and Cognito pools in that account and
region. Reuse a matching existing stack/pool, recording its pool and public client IDs.
Do not create a second pool just because the original is managed by another stack.
A matching pool must carry `Application=FieldEcho`, use case-insensitive email usernames,
disable self-sign-up and have only the five authorized members. Its public client must
use only Cognito, authorization code flow, `openid email`, no client secret, exactly
`https://fieldecho.org/api/auth/callback` and logout `https://fieldecho.org/`.
The provisioning command checks these constraints before changing users. Resolve a
mismatch privately with the resource owner; it deliberately does not retag or reconfigure
a pool to make it pass. Existing main's broad SAM template creates an entire different
infrastructure topology and is not required here.

Only if no existing pool is intended for this tenant, deploy the separate auth stack:

```sh
aws cloudformation deploy --template-file deploy/auth-bootstrap.yaml \
  --stack-name fieldecho-auth --region REGION \
  --parameter-overrides HostedDomainPrefix=CHOSEN-UNIQUE-PREFIX
aws cloudformation describe-stacks --stack-name fieldecho-auth --region REGION \
  --query 'Stacks[0].Outputs'
```

The stack puts the domain on Cognito managed login, which shows a FieldEcho-styled
sign-in page. Apply that style with the pool and client IDs from the stack outputs,
before anyone signs in. Apply it again after any change to `deploy/cognito-branding/`.
For an existing pool still on the old hosted page, apply the style before the stack
update that moves the domain to managed login:

```sh
uv run python deploy/cognito_branding.py --pool-id POOL_ID --client-id CLIENT_ID \
  --region REGION
```

Reuse this same stack name, account, region and domain prefix on subsequent runs. A
name/domain collision is a reason to stop and inspect ownership, not choose a fresh
name automatically. Retention policies preserve the pool, client and domain if the
stack is removed or replaced; deleting a stack is not account cleanup. Record the
resource IDs in the operator's private runbook. Do not remove/recreate the pool during
a release. No SES-backed email OTP or passwordless setup is assumed.

## Admit the five accounts

Create a UTF-8 JSON array of the five supplied email strings in a file outside every
Git checkout. It must be owned by the invoking operator and have mode `0600`. Do not
paste addresses into a shell command/history or commit them. The operator accepts
ordinary ASCII email addresses, preserves local-part spelling, lowercases the domain,
and rejects whitespace, duplicates (case insensitive), malformed addresses and empty lists before contacting AWS. It also rejects existing pool accounts
outside the authoritative list instead of deleting them.

```sh
uv run python deploy/cognito_admins.py --users-file /PRIVATE/PATH/operators.json \
  --account-id ACCOUNT_ID --region REGION --pool-id POOL_ID --client-id CLIENT_ID
```

The default suppresses invitation messages. Cognito generates temporary passwords;
the program never requests, stores or prints one. Existing users keep their passwords
and are never reinvited or reset on reruns. The group label is added only when absent.
The result contains counts, resource IDs and the four public runtime settings only.
AWS failure details are suppressed to avoid exposing participant addresses. Inspect
failures privately in AWS; partial success is safe to rerun with the same inputs.
Run one provisioning operator at a time; concurrent pool membership changes need a
fresh review. This is an allowlist preflight, not ongoing automated membership policing.

Sending invitations is a separate explicit operator action. Only after authorization
to send email, `--send-invitations` sends invitations to accounts newly created in that
run. It never resends to existing accounts. Accounts created with suppressed messages
remain pending onboarding; use the Cognito console's reviewed invitation/recovery flow
for those accounts after email sending is authorized. Do not delete accounts to force
this script to reinvite them. Temporary passwords expire after seven days.

Minimum operator Cognito permissions are `DescribeUserPool`, `DescribeUserPoolClient`,
`ListUsers`, `GetGroup`, `CreateGroup`, `AdminCreateUser`, `AdminListGroupsForUser` and
`AdminAddUserToGroup`, scoped to the selected pool. Bootstrap stack permissions are
separate; neither grants users AWS account access. Do not give CI these provisioning
permissions or the private account file.

## Cut over sign-in separately

Keep the existing Basic authentication until a real Cognito login has been verified.
The CLI returns these nonsecret settings:

- `MEL_AUTH_DOMAIN`: Cognito hosted UI HTTPS origin.
- `MEL_AUTH_CLIENT_ID`: public code-flow client ID.
- `MEL_AUTH_ISSUER`: selected pool's Cognito issuer URL.
- `MEL_CONSOLE_URL`: `https://fieldecho.org`.

Set all four together in the existing host runtime environment through the authorized
operator path. Keep that environment outside CI and preserve it on each release.
Current main's `/api/auth/login` uses authorization code with S256 PKCE and
`/api/auth/callback`; no client secret is needed. Main accepts only its pool's access
tokens issued for this client. Test a real invited operator's sign-in and console
access before removing Basic authentication through a separate reviewed cutover.
Also test logout and confirm an unsigned `/api/health` or other protected API returns
401 while static assets still load. Deployment readiness must use its direct database
probe and expected unsigned 401; do not add test tokens or bypass authentication.
The bootstrap alone does not prove sign-in or remove Basic authentication.

## Shared view-only login

Set `MEL_VIEWER_PASSWORD` in the existing private `explorer.env` on the host, alongside
the four Cognito settings, then restart explorer through the authorized operator path.
Choose a shared viewer password of 10–128 characters. Keep it out of Git,
CI variables, static files and logs; share it privately with viewers. An unset password
keeps the existing Cognito-only login. A viewer password without complete Cognito
settings is rejected at startup, so it cannot accidentally leave a local console open.

Visitors can choose **View only** with that password or **Sign in for full access**
with their existing Cognito account. Viewers land on Runs and can browse saved runs,
comparisons, survey previews, review lists and configuration versions. Editing,
generation, improvements, imports, deletion and spend-cap changes are hidden and
refused by the server with HTTP 403, including direct API calls. Configuration reads
do not seed or activate a baseline for viewers. Reading saved content still uses the
existing website and database infrastructure; it does not invoke paid models.
The Agents and Demo execution pages require full access. Their option discovery
contacts the paid AgentCore runtime, so even that GET request is refused for viewers.

Viewer sessions normally use a signed, Secure, HttpOnly, SameSite=Strict cookie valid
for eight hours. Anyone who knows the shared password can recreate a viewer cookie
with a later expiry. The expiry is a convenience, not a boundary on shared access;
server-enforced read-only permissions apply regardless of that expiry. Rotating or
removing the password and restarting explorer invalidates existing
viewer cookies. This is shared access: individual viewers cannot be identified or
revoked separately. Five incorrect passwords within one minute temporarily throttle
viewer login for the process; full-access sign-in and existing sessions remain available.
Multiple app processes need a shared proxy-level login rate limit.

Before enabling access, verify a viewer can open a saved run and comparison, cannot
start a batch or comparison or change the cap, and can sign out. Verify an operator
can still sign in through Cognito. If an outer Basic-auth gate remains, viewers must
also pass that gate; enabling this setting does not change nginx or CloudFront.
