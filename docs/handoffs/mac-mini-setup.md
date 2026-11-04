# Set up the Digital Green Explorer on mac-mini

You are running on my mac-mini. Set up the full Digital Green Pipeline Explorer so I can inspect an insight through the pipeline and try its survey in the phone. Execute the setup, not just a plan. This task works with any coding agent.

Repository: https://github.com/atredd23/AWS-breaking-barriers-DG37.git
Branch: ben/evals-and-checks
Minimum known working commit: 91dcab77ce552ce7eeb9654c8fa3ed4c3c39b320
Preferred checkout: ~/src/AWS-breaking-barriers-DG37

Read applicable host and repository instructions. Use docs/explorer.md, compose.yaml, and the actual source for the web app setup. docs/LOCAL_TESTING.md also covers the batch worker; do not confuse that CLI with the Explorer web service.

## Scope

- Start with recorded experiments. No AWS credentials or paid model calls are needed.
- Stay on ben/evals-and-checks. Never merge into main, delete branches, or push anything for this setup.
- Preserve existing files, checkouts, containers, volumes, Docker contexts, and unrelated services. Do not reset, stash, overwrite, prune, or delete other work.
- Reuse installed tools. Install only missing prerequisites using the existing package manager. Do not perform broad upgrades. Ask only for a genuine login or admin gate you cannot complete.
- Leave the requested Explorer and its PostgreSQL running after verification so I can use them. Stop temporary verification processes. Provide the exact stop command, retaining the database volume.
- Keep the API bound to loopback. Do not change public, Tailscale, or reverse-proxy exposure.

## Visual direction

Preserve this direction when reviewing the running app or doing later visual work:

- Render the question experience in an interactive FarmerChat phone. Let people select answers, move back and next, finish, and restart. Do not replace the player with a static image.
- Use a small agricultural field-photo header in the phone, with agricultural imagery around the pipeline to add warmth and context.
- Keep the farmer-facing survey branded green and white. Unselected answers have equal visual weight and white surfaces.
- Use restrained golden sunrise, purple, aqua, and coral accents for pipeline and data visuals. Let photography supply much of the extra color; avoid making every surface green or black.
- Use licensed, credited imagery. Make clear that illustrative photos do not depict the farmer or field in the selected synthetic case.

This is design context, not a request to redesign the app during installation. Get the existing interactive experience running first, and report any gap between the current visuals and this direction.

## Clone and inspect

Clone the repository with the named branch if the checkout is absent:

```bash
git clone --branch ben/evals-and-checks https://github.com/atredd23/AWS-breaking-barriers-DG37.git ~/src/AWS-breaking-barriers-DG37
```

If it exists, inspect its remote, branch, and dirty state before using it. Reuse it only when safe; otherwise use a separate clean checkout without disturbing it. Fetch the branch and update only by fast-forward. Confirm the checked-out branch includes the known working commit above. Report the actual HEAD.

## Start the full app

Check Git, Docker Compose, and a working Docker engine. Reuse Docker Desktop or Colima already configured on this Mac. Start the appropriate engine if needed without switching away from a healthy existing context.

Use a dedicated Compose project, dg37-explorer-mini. Check ports 8877 and 55437 first. If occupied by unrelated services, choose free ports and report them; do not kill anything.

From the checkout, with the chosen ports:

```bash
MEL_LOCAL_DATABASE_PORT=55437 MEL_EXPLORER_PORT=8877 \
  MEL_EXPLORER_ALLOWED_ORIGINS='["http://localhost:8877","http://127.0.0.1:8877","http://127.0.0.1:8878"]' \
  docker compose -p dg37-explorer-mini up --build -d --wait
```

The JSON allowlist includes the mac-mini browser on port `8877` and the
macdaddy-laptop tunnel browser on port `8878`. If you choose different ports,
update each origin to match. This override replaces Compose's derived `localhost`
and `127.0.0.1` defaults at `MEL_EXPLORER_PORT`. Other origins remain blocked.

The explorer service already uses mel-explorer --no-live. Keep live execution disabled. Open http://127.0.0.1:8877 on mac-mini. Import the recorded run. An unavailable AWS model catalog must not block the recorded demo.

## Verify the real experience

Require /api/health to report database connected, not merely HTTP 200. Require /api/config to show live_enabled false.

Use browser automation to import the recorded run and inspect pipeline stages, recorded events, drafts, review attempts, final rulings, and question evidence. Verify OpenAI Luna has two accepted questions, Anthropic Haiku has three, and Amazon Nova Lite retains its recorded execution failure. In the phone, test Start, required answers, Next, Back with retained choices, completion, and Restart. Check candidate changes reset the preview and failed candidates have no borrowed survey. Reanalyze saved outputs with rules and golden references without model calls.

For the repository browser check, install uv if missing and run:

```bash
uv sync --locked
uv run --locked python scripts/verify_explorer.py --url http://127.0.0.1:8877
```

The checker uses Google Chrome through Playwright. Reuse existing Chrome or install the missing browser prerequisite. Capture evidence outside the checkout. If a check fails, diagnose and report the actual failure; do not claim a pass from container startup alone. Do not change product behavior to make setup pass.

## Access and handoff

Report the actual branch and SHA, Compose project, ports, local URL, verification results, and any remaining gaps. Open the app in the browser on mac-mini. Leave it running for me.

For access from macdaddy-laptop, give this foreground SSH tunnel command, substituting the actual server port and a free local port:

```bash
ssh -N -L 8878:127.0.0.1:8877 mini
```

Then I open http://127.0.0.1:8878 on macdaddy-laptop. Explain that Ctrl-C stops the tunnel.

Give the stop command without -v:

```bash
docker compose -p dg37-explorer-mini down
```

Do not configure live Bedrock or copy AWS credentials from another Mac. After the recorded demo works, identify what would be needed for a separate live comparison on mac-mini. Do not invoke models until I explicitly request that step.
