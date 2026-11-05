FROM python:3.12-slim@sha256:b18b261e1b4974b4f321451ad5251ded85abb22c2b8b262da527dda1d031ab30
# Lambda runs this same image as the console API; the adapter extension forwards Lambda
# events to the HTTP server and is inert under ECS.
COPY --from=public.ecr.aws/awsguru/aws-lambda-adapter:0.9.1@sha256:46d6625e68cbbdd2efab4a20245977664513f13ffef47915b000d431adcea0b4 /lambda-adapter /opt/extensions/lambda-adapter
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
ARG FIELDECHO_REVISION=""
RUN printf '%s\n' "$FIELDECHO_REVISION" > /app/REVISION
COPY --from=ghcr.io/astral-sh/uv:0.10.12@sha256:72ab0aeb448090480ccabb99fb5f52b0dc3c71923bffb5e2e26517a1c27b7fec /uv /bin/uv
COPY pyproject.toml uv.lock ./
COPY src ./src
COPY dg_agent ./dg_agent
RUN UV_PROJECT_ENVIRONMENT=/usr/local uv sync --locked --no-dev --no-editable \
    && useradd --uid 10001 --create-home worker
COPY deploy/fieldecho.py deploy/target.json ./deploy/
COPY config ./config
COPY prompts ./prompts
COPY migrations ./migrations
COPY eval/golden_holdout.jsonl ./eval/golden_holdout.jsonl
# The console's Agents tab offers golden insights from both splits.
COPY eval/golden_dev.jsonl ./eval/golden_dev.jsonl
COPY docs/participant-pack/json/schemas ./docs/participant-pack/json/schemas
COPY docs/participant-pack/json/rules ./docs/participant-pack/json/rules
COPY docs/participant-pack/data/crop_calendar_bihar_rabi.csv ./docs/participant-pack/data/
RUN mkdir -p certs && python -c "import urllib.request; urllib.request.urlretrieve('https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem', 'certs/global-bundle.pem')"
USER 10001
ENTRYPOINT ["mel-worker"]
CMD ["--help"]
