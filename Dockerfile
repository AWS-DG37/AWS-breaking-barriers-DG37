FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
COPY dg_agent ./dg_agent
RUN pip install . && useradd --uid 10001 --create-home worker
COPY config ./config
COPY prompts ./prompts
COPY migrations ./migrations
COPY eval/golden_holdout.jsonl ./eval/golden_holdout.jsonl
COPY docs/participant-pack/json/schemas ./docs/participant-pack/json/schemas
COPY docs/participant-pack/json/rules ./docs/participant-pack/json/rules
COPY docs/participant-pack/data/crop_calendar_bihar_rabi.csv docs/participant-pack/data/insights.jsonl ./docs/participant-pack/data/
RUN mkdir -p certs && python -c "import urllib.request; urllib.request.urlretrieve('https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem', 'certs/global-bundle.pem')"
USER 10001
ENTRYPOINT ["mel-worker"]
CMD ["--help"]
