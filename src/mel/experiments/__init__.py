from __future__ import annotations

import asyncio
import copy
import json
from collections import Counter
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from pydantic import Field, ValidationError, model_validator

from dg_agent.checks import check_survey
from dg_agent.checks import rules as reference_rules
from dg_agent.models import Insight, Survey
from mel.agents import build_agents
from mel.bundle import Bundle
from mel.config import AgentConfig, Settings, StrictModel
from mel.configs import config_bundle
from mel.contracts import Contracts
from mel.costs import meter_agents
from mel.decision import DecisionEngine
from mel.graph import Dependencies
from mel.models import Outcome, TraceEvent
from mel.store import Store
from mel.worker import close_model_clients, process_insight


class ExperimentCandidate(StrictModel):
    """One configuration version under comparison. Recorded runs, saved before versions
    existed, have no config_id; each run's manifest carries its configuration."""

    label: str = Field(min_length=1, max_length=120, pattern=r"\S")
    config_id: UUID | None = None


class ExperimentSpec(StrictModel):
    name: str = Field(min_length=1, max_length=200, pattern=r"\S")
    # Resolved from a dataset selection when the experiment is created, so every
    # candidate sees exactly the same insights.
    insights: list[dict] = Field(min_length=1, max_length=50)
    candidates: list[ExperimentCandidate] = Field(min_length=1, max_length=6)
    evaluation_reference: Literal["rules", "golden"] = "rules"
    budget_usd: Decimal | None = Field(default=None, gt=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def identities(self):
        ids = [r.get("insight_id") for r in self.insights]
        if any(not isinstance(key, str) or not key.strip() for key in ids):
            raise ValueError("each insight needs a nonempty insight_id")
        if len(set(ids)) != len(ids):
            raise ValueError("insight IDs must be unique")
        if len({c.label for c in self.candidates}) != len(self.candidates):
            raise ValueError("candidate labels must be unique")
        if any("expected" in row or "insight" in row for row in self.insights):
            raise ValueError("provide normalized insights, without evaluation targets")
        return self


class QualityAssessment(StrictModel):
    scope: Literal["question", "survey", "run"]
    assessor: Literal["code", "model", "human"]
    run_id: str = Field(min_length=1)
    insight_id: str | None = None
    item_id: str | None = None
    dimension: str = Field(min_length=1, max_length=120)
    rubric_version: str = Field(min_length=1, max_length=120)
    assessment: Literal["assessed", "not_assessed"]
    score: float | None = Field(default=None, allow_inf_nan=False)
    note: str = Field(default="", max_length=4000)

    @model_validator(mode="after")
    def scoped_reference(self):
        expected = {"run": (False, False), "survey": (True, False), "question": (True, True)}
        if (bool(self.insight_id), bool(self.item_id)) != expected[self.scope]:
            raise ValueError("assessment reference must match its scope")
        if self.assessment == "not_assessed" and self.score is not None:
            raise ValueError("not_assessed cannot have a score")
        if self.assessment == "assessed" and self.score is None:
            raise ValueError("assessed requires the explicitly provided score")
        return self


def validate_assessment(detail: dict, assessment: QualityAssessment) -> None:
    run = next((r for r in detail["runs"] if r["run_id"] == assessment.run_id), None)
    if run is None:
        raise ValueError("run does not belong to this experiment")
    if assessment.scope == "run":
        return
    result = next((r for r in run["results"] if r["insight_id"] == assessment.insight_id), None)
    if result is None:
        raise ValueError("insight does not belong to this run")
    outcome = result["outcome"]
    survey = outcome.get("survey") or outcome.get("candidate_survey")
    if survey is None:
        raise ValueError("this result has no survey to assess")
    if assessment.scope == "question" and not any(
        item["item_id"] == assessment.item_id for item in survey["items"]
    ):
        raise ValueError("question does not belong to this survey")


def resolve_context(insight: dict, profiles: list[dict], calendar: list[dict]) -> dict:
    matches = [p for p in profiles if p.get("farmer_id") == insight.get("farmer_id")]
    profile = copy.deepcopy(matches[0]) if matches else None
    crop = insight.get("asset", {}).get("name")
    rows = [copy.deepcopy(r) for r in calendar if crop is not None and r.get("crop") == crop]
    conflicts = []
    if len(matches) > 1:
        conflicts.append("Multiple farmer profiles matched. The first supplied profile is shown.")
    if profile:
        language = profile.get("conversation_language", profile.get("language"))
        if language and language != insight.get("conversation_language"):
            conflicts.append("Profile language differs. Insight language governs generation.")
        crops = profile.get("crops")
        if crop and crops and crop not in crops:
            conflicts.append(
                "Insight crop differs from the profile crop list. Insight crop governs."
            )
        if not crop and crops:
            conflicts.append("Insight crop is absent. Profile crops are not used to infer a crop.")
    return {
        "profile": profile,
        "crop_calendar": rows,
        "conflicts": conflicts,
        "sources": [
            {"source": "insight", "fields": ["asset", "advice_summary", "conversation_language"]},
            {"source": "profile", "matched": bool(matches)},
            {"source": "crop_calendar", "crop": crop, "matched_rows": len(rows)},
        ],
    }


async def execute_experiment(
    spec: ExperimentSpec,
    *,
    settings: Settings,
    store: Store,
    experiment_id: str | None = None,
    job_id: str | None = None,
    agent_factory=build_agents,
    on_progress=None,
) -> str:
    """Run every candidate configuration version over the same insights, through the
    same graph, and record each run against its version. `on_progress(done, total)`
    counts insight results across all candidates and runs in a worker thread."""
    if any(c.config_id is None for c in spec.candidates):
        raise ValueError("live experiments compare saved configuration versions")
    key = experiment_id or str(uuid4())
    await asyncio.to_thread(store.create_experiment, key, spec.model_dump(mode="json"))
    await asyncio.to_thread(store.finish_experiment, key, "running")
    try:
        rows = spec.insights
        total = len(rows) * len(spec.candidates)
        had_errors = False

        async def progress(done: int) -> None:
            if on_progress is not None:
                await asyncio.to_thread(on_progress, done, total)

        for index, candidate in enumerate(spec.candidates):
            run_had_errors = False
            run_id = str(uuid5(NAMESPACE_URL, f"{key}/{index}"))
            config_id = str(candidate.config_id)
            bundle = await asyncio.to_thread(config_bundle, store, config_id)
            config = bundle.config
            snapshot = {
                **copy.deepcopy(bundle.snapshot),
                "label": candidate.label,
                "contexts": {
                    r["insight_id"]: resolve_context(r, [], bundle.snapshot["calendar"])
                    for r in rows
                },
                "selection": {"selected_rows": len(rows)},
                "bedrock_project_id": settings.bedrock_project_id,
            }
            candidate_bundle = Bundle(config, snapshot, bundle.contracts)
            fingerprint = candidate_bundle.fingerprint(
                json.dumps(rows, sort_keys=True).encode(), settings.aws_region
            )
            await asyncio.to_thread(
                store.start_run,
                run_id,
                fingerprint,
                snapshot,
                purpose="experiment",
                experiment_id=key,
                config_id=config_id,
                job_id=job_id,
            )
            agents = ()
            try:
                agents = agent_factory(
                    config, settings.aws_region, snapshot["prompts"], settings.bedrock_project_id
                )
                if hasattr(store, "connection"):
                    meter_agents(
                        store,
                        run_id,
                        config,
                        settings.aws_region,
                        agents,
                        spec.budget_usd,
                        capped=job_id is not None,  # Console jobs count toward the spend cap.
                    )
                deps = Dependencies.from_bundle(candidate_bundle, *agents)
                completed = await asyncio.to_thread(store.completed, run_id, False)
                finished = len(completed)
                await progress(index * len(rows) + finished)
                for ordinal, row in enumerate(rows, 1):
                    if row["insight_id"] in completed:
                        continue
                    outcome = await process_insight(
                        row,
                        f"S-{ordinal:010d}",
                        deps,
                        context=snapshot["contexts"][row["insight_id"]],
                    )
                    await asyncio.to_thread(store.save, run_id, row, outcome)
                    finished += 1
                    await progress(index * len(rows) + finished)
                    run_had_errors |= outcome.status == "failed"
                    had_errors |= run_had_errors
                persisted = await asyncio.to_thread(store.results, run_id)
                run_had_errors = any(o["status"] == "failed" for o in persisted)
                had_errors |= run_had_errors
                await asyncio.to_thread(
                    store.finish, run_id, "completed_with_errors" if run_had_errors else "completed"
                )
            except BaseException as error:
                had_errors = True
                completed = await asyncio.to_thread(store.completed, run_id, False)
                for row in rows:
                    if row["insight_id"] not in completed:
                        failed = Outcome(
                            insight_id=row["insight_id"],
                            status="failed",
                            reason=type(error).__name__,
                            usage_complete=False,
                            events=[
                                TraceEvent(
                                    sequence=1,
                                    stage="failure",
                                    source="runtime",
                                    attempt=0,
                                    data={"error_type": type(error).__name__},
                                )
                            ],
                        )
                        await asyncio.to_thread(store.save, run_id, row, failed)
                await asyncio.to_thread(store.finish, run_id, "failed")
                await progress((index + 1) * len(rows))
                if not isinstance(error, Exception):
                    raise
            finally:
                await close_model_clients(*agents)
        await asyncio.to_thread(
            store.finish_experiment, key, "completed_with_errors" if had_errors else "completed"
        )
    except BaseException:
        await asyncio.to_thread(store.finish_experiment, key, "failed")
        raise
    return key


def _reference_engine(manifest: dict) -> DecisionEngine | None:
    """The decision engine a run was recorded with. Runs recorded before the engine
    existed carry no decision policy, so they have no rules reference."""
    if "decision" not in manifest.get("config", {}):
        return None
    config = AgentConfig.model_validate(manifest["config"])
    return Bundle(config, manifest, Contracts(schemas=manifest["schemas"])).decision_engine()


def _policy_reference(insight: dict, engine: DecisionEngine) -> dict:
    decision = engine.decide(insight)
    if not decision.survey:
        return {"survey": False, "reason": decision.reason_code}
    return {"survey": True, "send_window": decision.window.to_contract()}


def reference_checks(insight: dict, outcome: dict) -> dict:
    evidence = {
        "assessor": "code",
        "scope": "survey",
        "reference": "legacy participant pack",
        "assessment": "not_assessed",
        "checks": {},
        "limitations": (
            "Language checks compare metadata. Advice and PII patterns cover "
            "English only. This diagnostic does not change runtime acceptance."
        ),
    }
    survey = outcome.get("survey") or outcome.get("candidate_survey")
    if survey is None:
        return evidence
    try:
        source = Insight.model_validate(insight)
        checks = check_survey(Survey.model_validate(survey), {source.insight_id: source})
    except ValidationError:
        return {**evidence, "reason": "Record is incompatible with the reference model"}
    return {
        **evidence,
        "assessment": "assessed",
        "checks": checks,
        "rules_sha256": sha256(json.dumps(reference_rules(), sort_keys=True).encode()).hexdigest(),
    }


def compare_runs(detail: dict, *, reference: Literal["rules", "golden"] | None = None) -> dict:
    spec = detail["spec"]
    reference = reference or spec.get("evaluation_reference", "rules")
    selected = spec.get("insights", [])
    golden = {}
    reference_snapshot = {"source": "each saved run manifest"}
    if reference == "golden":
        path = Path("eval/golden_holdout.jsonl")
        reference_snapshot = {"source": str(path), "sha256": None}
        if path.exists():
            reference_snapshot["sha256"] = sha256(path.read_bytes()).hexdigest()
            golden = {
                r["insight_id"]: r
                for r in (
                    json.loads(line) for line in path.read_text().splitlines() if line.strip()
                )
            }
    rows, cases, evidence = [], [], []
    runs = list(detail.get("runs", []))
    labels = {r["label"] for r in runs}
    runs.extend(
        {
            "label": candidate["label"],
            "model_id": None,  # Known once the candidate's run starts.
            "results": [],
            "manifest": {},
        }
        for candidate in spec.get("candidates", [])
        if candidate["label"] not in labels
    )
    for run in runs:
        results = run.get("results", [])
        engine = _reference_engine(run.get("manifest", {})) if reference == "rules" else None
        counts = Counter(r["outcome"]["status"] for r in results)
        total = len(selected) or len(results)
        complete = len(results) == total and all(
            r["outcome"].get("usage_complete", False) for r in results
        )
        scores = [
            r["outcome"]["review"]["relevance_score"] for r in results if r["outcome"].get("review")
        ]
        rows.append(
            {
                "run_id": run.get("run_id"),
                "label": run["label"],
                "model_id": run.get("model_id"),
                "total": total,
                **{s: counts[s] for s in ("accepted", "skipped", "rejected", "failed")},
                "missing": max(0, total - len(results)),
                "usage_complete": complete,
                "input_tokens": sum(r["outcome"].get("input_tokens", 0) for r in results)
                if complete
                else None,
                "output_tokens": sum(r["outcome"].get("output_tokens", 0) for r in results)
                if complete
                else None,
                "mean_relevance": sum(scores) / len(scores) if scores else None,
                "relevance_count": len(scores),
                "relevance_scope": "survey",
                "relevance_assessor": "model",
            }
        )
        for result in results:
            insight = result["input"]
            target = golden.get(insight["insight_id"])
            expected = target["expected"] if target and target.get("insight") == insight else None
            if reference == "rules":
                expected = _policy_reference(insight, engine) if engine else None
            outcome = result["outcome"]
            observed = outcome["status"] == "accepted"
            assessed = expected is not None and outcome["status"] not in ("failed", "rejected")
            actual_window = (outcome.get("survey") or {}).get("send_window")
            expected_window = expected.get("send_window") if expected else None
            if expected_window and "start" in expected_window:
                expected_window = {
                    "not_before": expected_window["start"],
                    "not_after": expected_window["end"],
                }
            reason_expected = (
                expected.get("reason", expected.get("reason_code")) if expected else None
            )
            cases.append(
                {
                    "run_id": run.get("run_id"),
                    "insight_id": insight["insight_id"],
                    "scope": "survey",
                    "assessor": "code",
                    "reference": reference,
                    "deterministic_checks": reference_checks(insight, outcome),
                    "assessment": "assessed" if assessed else "not_assessed",
                    "expected": expected,
                    "observed_status": outcome["status"],
                    "agreement": observed == expected["survey"] if assessed else None,
                    "decision_agreement": observed == expected["survey"] if assessed else None,
                    "reason_agreement": outcome.get("reason") == reason_expected
                    if assessed and reason_expected
                    else None,
                    "window_agreement": actual_window == expected_window
                    if assessed and expected_window
                    else None,
                }
            )
            if outcome.get("review"):
                evidence.append(
                    {
                        "scope": "survey",
                        "assessor": "model",
                        "run_id": run.get("run_id"),
                        "insight_id": insight["insight_id"],
                        "dimension": "relevance",
                        "score": outcome["review"]["relevance_score"],
                        "assessment": "assessed",
                    }
                )
    return {
        "reference": reference,
        "reference_snapshot": reference_snapshot,
        "rows": rows,
        "cases": cases,
        "assessments": [*evidence, *detail.get("assessments", [])],
        "notes": [
            "Relevance is a survey-wide model judgment, not independent quality validation.",
            "Missing usage and missing assessments remain unknown.",
            "Golden labels are analysis-only and never passed to generation.",
        ],
    }
