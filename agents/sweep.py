#!/usr/bin/env python3
"""Model sweep harness: run the MEL survey pipeline across candidate generator models
and score each run against the golden set.

Two pipelines:
  direct     gate -> generator writes the survey in the farmer's language -> code checks -> verifier
  translate  gate -> generator writes English -> translator writes the farmer's language
             (translation cache checked first) -> code checks -> verifier

Each role has its own model: --models (generators, one run per model), --translator, and
--verifier-model (held fixed across a sweep so the generator is the variable).

Per insight it records the decision vs golden, code-check errors, verifier verdict and relevance,
tokens and seconds for each stage (generate / translate / verify), translation cache hits, and the
final survey items (plus the English draft in translate mode) for side-by-side reading.

Usage:
    python sweep.py --golden <golden_set.jsonl> --models global.anthropic.claude-haiku-5-5 \\
        --pipeline translate --translator google.gemma-3-27b-it --limit 10

Each run writes runs/<timestamp>__<pipeline>__<model>.json and prints a comparison table.
Env: AWS_REGION (default us-east-1), MAX_TOKENS / VERIFIER_MAX_TOKENS / TRANSLATOR_MAX_TOKENS,
TEMPERATURE (generator, opt-in).
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from run_one import (  # noqa: E402
    HINDI, blocking_issues, build_user, build_verifier_user, check, decide, effective_verdict,
    load_prompts, load_retry_template, load_verifier_prompts, parse_json, verdict_issues,
)
import translate  # noqa: E402
from decide import REMOVED_RULES, TIMING_RULES  # noqa: E402  (path set up by run_one)

DEFAULT_VERIFIER = "global.anthropic.claude-sonnet-5-5"
DEFAULT_TRANSLATOR = "google.gemma-3-27b-it"
# Verifier checks whose problems are about wording, so the translator can fix them
# without regenerating the English survey.
WORDING_CHECKS = {"language", "language_ok", "voice", "voice_ok", "fidelity", "fidelity_ok"}
STAGES = ("generate", "translate", "verify")


def _call(role, model_id, system, user):
    """One model call through Strands. Returns (text, input_tokens, output_tokens, seconds)."""
    from strands import Agent
    from strands.models import BedrockModel
    env_cap = {"generate": "MAX_TOKENS", "verify": "VERIFIER_MAX_TOKENS",
               "translate": "TRANSLATOR_MAX_TOKENS"}[role]
    kwargs = {"model_id": model_id, "region_name": os.environ.get("AWS_REGION", "us-east-1"),
              "max_tokens": int(os.environ.get(env_cap, "8000"))}
    if role == "generate" and os.environ.get("TEMPERATURE"):
        kwargs["temperature"] = float(os.environ["TEMPERATURE"])
    started = time.monotonic()
    result = Agent(model=BedrockModel(**kwargs), system_prompt=system, callback_handler=None)(user)
    seconds = time.monotonic() - started
    try:
        u = result.metrics.accumulated_usage
        tin, tout = int(u.get("inputTokens", 0)), int(u.get("outputTokens", 0))
    except Exception:
        tin = tout = 0
    return str(result), tin, tout, seconds


def _glossary(insight, lang):
    name = insight["asset"]["name"]
    return f"{name} = {HINDI[name]}" if lang == "hi-IN" and name in HINDI else "(not needed)"


def run_pipeline(insight, generator, verifier=DEFAULT_VERIFIER, pipeline="direct",
                 translator=DEFAULT_TRANSLATOR, max_retries=1, use_translation_cache=True):
    """Run ONE insight through the chosen pipeline. Returns a result dict."""
    started = time.monotonic()
    d = decide(insight)
    stages = {s: {"calls": 0, "input_tokens": 0, "output_tokens": 0, "seconds": 0.0} for s in STAGES}
    stages["translate"]["cache_hits"] = 0
    out = {
        "insight_id": insight["insight_id"], "resolution_type": insight["resolution_type"],
        "language": insight["conversation_language"], "pipeline": pipeline,
        "decision_rule": d["rule"], "decision_survey": d["survey"],
        "status": None, "verdict": None, "relevance": None, "attempts": 0,
        "code_errors": [], "items": None, "items_en": None, "error": None, "stages": stages,
    }

    def call(stage, model_id, system, user):
        text, tin, tout, secs = _call(stage, model_id, system, user)
        st = stages[stage]
        st["calls"] += 1; st["input_tokens"] += tin; st["output_tokens"] += tout
        st["seconds"] += secs
        return text

    def finish(status):
        out["status"] = status
        for st in stages.values():
            st["seconds"] = round(st["seconds"], 3)
        # flat per-stage token totals, also read by older run files
        for stage, key in (("generate", "gen_tokens"), ("verify", "ver_tokens"),
                           ("translate", "tr_tokens")):
            s = stages[stage]
            out[key] = {"input_tokens": s["input_tokens"], "output_tokens": s["output_tokens"],
                        "total_tokens": s["input_tokens"] + s["output_tokens"]}
        out["elapsed_seconds"] = round(time.monotonic() - started, 3)
        return out

    if not d["survey"]:
        return finish("skipped")

    target = insight["conversation_language"]
    translating = pipeline == "translate" and target != "en-IN"
    # In translate mode the generator writes English: same prompt, language set to en-IN.
    gen_insight = insight
    if pipeline == "translate":
        gen_insight = copy.deepcopy(insight)
        gen_insight["conversation_language"] = "en-IN"

    system, tpl = load_prompts()
    base_user = build_user(tpl, gen_insight)
    retry_tpl = load_retry_template()
    vsys, vtpl = load_verifier_prompts()
    tsys, ttpl, tretry = translate.load_prompts()

    gen_user = base_user
    gen = None               # parsed generator output for the current English/direct draft
    need_generate = True
    tr_issues = None         # set when the next round should only redo the translation
    prev_translation = None
    verdict = None
    try:
        for attempt in range(max_retries + 1):
            out["attempts"] = attempt + 1
            verdict = None

            # 1. Generate (skipped when only the translation needs fixing)
            if need_generate:
                raw = call("generate", generator, system, gen_user)
                try:
                    gen = parse_json(raw)
                except json.JSONDecodeError as e:
                    gen = None
                    gen_user = base_user + "\n\n" + retry_tpl.format(
                        issues_list=f"Output was not valid JSON: {e}", previous_items_json=raw[:3000])
                    continue
                errors, _ = check(gen, gen_insight)
                out["code_errors"] = errors
                if pipeline == "translate":
                    out["items_en"], out["items"] = gen.get("items"), None  # old translation is stale
                else:
                    out["items"] = gen.get("items")
                if errors:
                    gen_user = base_user + "\n\n" + retry_tpl.format(
                        issues_list="\n".join("Code check: " + x for x in errors),
                        previous_items_json=json.dumps(gen.get("items", []), ensure_ascii=False))
                    continue
            final_items = gen["items"]

            # 2. Translate (cache first, unless we are fixing a flagged translation)
            if translating:
                hit = None
                if use_translation_cache and tr_issues is None and attempt == 0:
                    hit = translate.cache_get(translator, target, gen["items"])
                if hit is not None:
                    stages["translate"]["cache_hits"] += 1
                    final_items = hit
                else:
                    tuser = ttpl.format(
                        target_language=target, glossary_lines=_glossary(insight, target),
                        english_items_json=json.dumps(translate.text_only(gen["items"]),
                                                      ensure_ascii=False, indent=1))
                    if tr_issues:
                        tuser += "\n\n" + tretry.format(
                            issues_list="\n".join(tr_issues),
                            previous_items_json=json.dumps(prev_translation, ensure_ascii=False))
                    raw = call("translate", translator, tsys, tuser)
                    try:
                        parsed = translate.parse_loose(raw)
                    except json.JSONDecodeError as e:
                        tr_issues, prev_translation, need_generate = (
                            [f"Output was not valid JSON: {e}"], raw[:2000], False)
                        continue
                    final_items, problems = translate.merge(gen["items"], parsed)
                    prev_translation = translate.text_only(final_items)
                    if problems:
                        out["items"], out["code_errors"] = final_items, problems
                        tr_issues, need_generate = problems, False
                        continue
            out["items"] = final_items

            # 3. Code checks on what the farmer will see
            final = {"items": final_items, "rationale": gen.get("rationale", ""),
                     **({"self_eval": gen["self_eval"]} if gen.get("self_eval") else {})}
            errors, _ = check(final, insight)
            out["code_errors"] = errors
            if errors:
                if translating:   # the English passed the same checks, so the translation broke it
                    tr_issues, prev_translation, need_generate = (
                        ["Code check: " + x for x in errors], translate.text_only(final_items), False)
                else:
                    gen_user = base_user + "\n\n" + retry_tpl.format(
                        issues_list="\n".join("Code check: " + x for x in errors),
                        previous_items_json=json.dumps(final_items, ensure_ascii=False))
                    need_generate = True
                continue

            # 4. Verify the final survey in the farmer's language
            vraw = call("verify", verifier, vsys, build_verifier_user(vtpl, insight, final_items))
            try:
                verdict = parse_json(vraw)
            except json.JSONDecodeError:
                try:
                    verdict = translate.parse_loose(vraw)
                except json.JSONDecodeError:
                    verdict = {"verdict": "fail", "relevance": None,
                               "issues": [{"problem": "verifier output was not JSON"}]}
            verdict["model_verdict"], verdict["verdict"] = verdict.get("verdict"), effective_verdict(verdict)
            out.setdefault("reviews", []).append({   # keep every review for later analysis
                "attempt": attempt + 1, "verdict": verdict["verdict"],
                "model_verdict": verdict["model_verdict"],
                "relevance": verdict.get("relevance"), "checks": verdict.get("checks"),
                "issues": verdict.get("issues"),
                # the verifier's English reading of each question; the console shows it on request
                "back_translation": verdict.get("back_translation")})
            if verdict["verdict"] in ("pass", "fail"):
                break
            issues = verdict_issues(verdict)
            flagged = {x.get("check") for x in blocking_issues(verdict)}
            if translating and flagged and flagged <= WORDING_CHECKS:
                # only the wording is wrong: re-translate, keep the English survey
                tr_issues, prev_translation, need_generate = (
                    issues, translate.text_only(final_items), False)
            else:
                tr_issues, need_generate = None, True
                prev = gen["items"]
                gen_user = base_user + "\n\n" + retry_tpl.format(
                    issues_list="\n".join(issues), previous_items_json=json.dumps(prev, ensure_ascii=False))
    except Exception as e:   # a model/transport failure should not stop the whole sweep
        out["error"] = f"{type(e).__name__}: {e}"
        return finish("failed")

    final_verdict = (verdict or {}).get("verdict")
    out["verdict"] = final_verdict
    out["relevance"] = (verdict or {}).get("relevance")
    if final_verdict == "pass" and translating and use_translation_cache and gen:
        translate.cache_put(translator, target, gen["items"], out["items"])  # cache only verified text
    return finish("accepted" if final_verdict == "pass" else "rejected")


def _stage(r, stage):
    """Stage stats from a result, tolerating run files written before stages existed."""
    if r.get("stages"):
        return r["stages"][stage]
    key = {"generate": "gen_tokens", "verify": "ver_tokens", "translate": "tr_tokens"}[stage]
    t = r.get(key) or {}
    return {"calls": 0, "input_tokens": t.get("input_tokens", 0),
            "output_tokens": t.get("output_tokens", 0), "seconds": 0.0, "cache_hits": 0}


def score_run(results, golden_by_id):
    """Aggregate per-insight results against golden expectations."""
    n = len(results)
    generated = [r for r in results if r["status"] in ("accepted", "rejected")]
    # Golden rows for rules the agents do not apply (R2 removed; R6/R7 belong to the timing
    # script) are not scored as decision errors.
    elsewhere = REMOVED_RULES | TIMING_RULES
    decision_hits = rule_hits = decision_cases = 0
    for r in results:
        g = golden_by_id.get(r["insight_id"])
        if g and g["expected"]["rule"] not in elsewhere:
            decision_cases += 1
            decision_hits += int((r["status"] in ("accepted", "rejected", "failed"))
                                 == bool(g["expected"]["survey"]))
            rule_hits += int(r["decision_rule"] == g["expected"]["rule"])
    rel = [r["relevance"] for r in results if r.get("relevance") is not None]
    item_counts = [len(r["items"]) for r in results if r.get("items")]
    accepted = sum(r["status"] == "accepted" for r in results)
    status_counts = {}
    for r in results:
        status_counts[r["status"]] = status_counts.get(r["status"], 0) + 1
    tokens, seconds = {}, {}
    for s in STAGES:
        tokens[s] = {"input": sum(_stage(r, s)["input_tokens"] for r in results),
                     "output": sum(_stage(r, s)["output_tokens"] for r in results)}
        secs = [_stage(r, s)["seconds"] for r in generated]
        seconds[s] = round(sum(secs) / len(secs), 3) if secs else None
    elapsed = [r["elapsed_seconds"] for r in generated if r.get("elapsed_seconds") is not None]

    def rate(h, t):
        return round(h / t, 4) if t else None

    total = sum(v["input"] + v["output"] for v in tokens.values())
    return {
        "cases": n,
        "status_counts": status_counts,
        "decision_accuracy": rate(decision_hits, decision_cases),
        "rule_accuracy": rate(rule_hits, decision_cases),
        "decision_cases": decision_cases,
        "accept_rate": rate(accepted, len(generated)),
        "pass_rate_over_all": rate(accepted, n),
        "relevance_mean": round(sum(rel) / len(rel), 4) if rel else None,
        "items_mean": round(sum(item_counts) / len(item_counts), 3) if item_counts else None,
        "attempts_mean": round(sum(r["attempts"] for r in generated) / len(generated), 3)
        if generated else None,
        "tokens": {**tokens, "total": total,
                   "per_survey": round(total / len(generated)) if generated else None,
                   "gen_input": tokens["generate"]["input"], "gen_output": tokens["generate"]["output"],
                   "ver_input": tokens["verify"]["input"], "ver_output": tokens["verify"]["output"]},
        "seconds_per_survey_by_stage": seconds,
        "seconds_per_survey_mean": round(sum(elapsed) / len(elapsed), 3) if elapsed else None,
        "seconds_per_case_mean": round(sum(elapsed) / len(elapsed), 3) if elapsed else None,
        "seconds_total": round(sum(r.get("elapsed_seconds") or 0 for r in results), 3),
        "translation_cache_hits": sum(_stage(r, "translate").get("cache_hits", 0) for r in results),
    }


def load_golden(path):
    return [json.loads(l) for l in Path(path).read_text(encoding="utf-8").splitlines() if l.strip()]


def prompt_fingerprint():
    """Short hash of the prompt files in use, so run files show which prompt version produced them."""
    import hashlib
    from run_one import prompt_path
    h = hashlib.sha1()
    for name in ("generator_prompt.md", "verifier_prompt.md", "translator_prompt.md"):
        h.update(Path(prompt_path(name)).read_bytes())
    return h.hexdigest()[:10]


def run_model(model_id, verifier_model_id, golden_rows, limit, max_retries, out_dir,
              progress=None, pipeline="direct", translator_model_id=DEFAULT_TRANSLATOR,
              use_translation_cache=True, tag=None, golden_name=None):
    """Run one generator model over the first `limit` golden insights; write and return the run."""
    golden_by_id = {g["insight"]["insight_id"]: g for g in golden_rows}
    selected = golden_rows[:limit]
    results = []
    for i, g in enumerate(selected, 1):
        r = run_pipeline(g["insight"], model_id, verifier_model_id, pipeline, translator_model_id,
                         max_retries, use_translation_cache)
        results.append(r)
        if progress:
            progress(model_id, i, len(selected), r)
    run = {
        "model_id": model_id,
        "pipeline": pipeline,
        "tag": tag,
        "prompt_dir": os.environ.get("MEL_PROMPT_DIR"),
        "prompt_sha": prompt_fingerprint(),
        "golden": golden_name,
        "translator_model_id": translator_model_id if pipeline == "translate" else None,
        "verifier_model_id": verifier_model_id,
        "region": os.environ.get("AWS_REGION", "us-east-1"),
        "limit": len(selected),
        "max_retries": max_retries,
        "use_translation_cache": use_translation_cache,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "summary": score_run(results, golden_by_id),
        "results": results,
    }
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", model_id)
    label = f"{pipeline}-{re.sub(r'[^A-Za-z0-9._-]', '_', tag)}" if tag else pipeline
    path = out_dir / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}__{label}__{safe}.json"
    path.write_text(json.dumps(run, ensure_ascii=False, indent=2), encoding="utf-8")
    run["_path"] = str(path)
    return run


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--golden", required=True, help="golden_set.jsonl path")
    ap.add_argument("--models", nargs="+", required=True, help="generator model IDs to compare")
    ap.add_argument("--pipeline", choices=("direct", "translate"), default="direct")
    ap.add_argument("--translator", default=DEFAULT_TRANSLATOR, help="translator model (translate pipeline)")
    ap.add_argument("--verifier-model", default=DEFAULT_VERIFIER, help="fixed verifier model")
    ap.add_argument("--limit", type=int, default=10, help="golden insights per model")
    ap.add_argument("--max-retries", type=int, default=1, help="revise rounds after the first")
    ap.add_argument("--no-translation-cache", action="store_true")
    ap.add_argument("--out", default="runs", help="output directory for run files")
    ap.add_argument("--prompt-dir", help="folder of prompt variants (generator_prompt.md, "
                    "verifier_prompt.md, translator_prompt.md); missing files use the defaults")
    ap.add_argument("--tag", help="label for this run, e.g. the prompt variant name")
    a = ap.parse_args()
    if a.prompt_dir:
        if not Path(a.prompt_dir).is_dir():
            ap.error(f"--prompt-dir {a.prompt_dir} is not a folder")
        os.environ["MEL_PROMPT_DIR"] = str(Path(a.prompt_dir).resolve())
    golden_rows = load_golden(a.golden)

    def progress(model, i, total, r):
        print(f"[{model}] {i}/{total} {r['insight_id']} -> {r['status']}"
              f" (verdict={r.get('verdict')}, rel={r.get('relevance')}, attempts={r['attempts']})",
              flush=True)

    runs = []
    for m in a.models:
        print(f"\n=== {a.pipeline}: generator {m} ===", flush=True)
        runs.append(run_model(m, a.verifier_model, golden_rows, a.limit, a.max_retries, a.out,
                              progress, a.pipeline, a.translator, not a.no_translation_cache,
                              a.tag, Path(a.golden).name))
    print(f"\n{'generator':45} {'accept':>7} {'relev':>6} {'tok/srv':>8} {'s/srv':>6}")
    for run in runs:
        s = run["summary"]
        print(f"{run['model_id']:45} {str(s['accept_rate']):>7} {str(s['relevance_mean']):>6} "
              f"{str(s['tokens']['per_survey']):>8} {str(s['seconds_per_survey_mean']):>6}")


if __name__ == "__main__":
    main()
