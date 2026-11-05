#!/usr/bin/env python3
"""Run the MEL survey pipeline on ONE insight: gate -> generator (Strands) -> code checks -> verifier.

Usage:
  python run_one.py data/insights.jsonl --index 0 --dry-run     # no AWS needed, tests the plumbing
  python run_one.py data/insights.jsonl --index 0               # Strands agent (Bedrock + AWS MCP tools)
  python run_one.py data/insights.jsonl --id INS-XXXX
Env (optional): AWS_REGION (default us-east-1), MODEL_ID (default global.anthropic.claude-haiku-5-5),
                TEMPERATURE (leave unset first; set e.g. 0.3 if the model accepts it)
"""
import argparse, glob, json, os, re, sys

ROOT = os.path.dirname(os.path.abspath(__file__))
RULES = os.path.join(ROOT, "skills", "mel-survey-rules")
WRITER = os.path.join(ROOT, "skills", "mel-survey-writer")
sys.path.insert(0, os.path.join(RULES, "scripts"))
sys.path.insert(0, os.path.join(WRITER, "scripts"))
from decide import decide          # noqa: E402
from check_survey import check     # noqa: E402
sys.path.insert(0, ROOT)
import cache                       # noqa: E402

HINDI = {"wheat": "गेहूं", "mustard": "सरसों", "lentil": "मसूर", "potato": "आलू", "maize": "मक्का",
         "gram": "चना", "pea": "मटर", "onion": "प्याज", "tomato": "टमाटर", "cauliflower": "फूलगोभी",
         "cabbage": "पत्तागोभी", "goat": "बकरी", "cow": "गाय", "buffalo": "भैंस"}
EXAMPLE_FOR = {"direct_action": "1-mustard-aphid-spray", "wait_and_monitor": "3-potato-blight-wait",
               "escalate_to_expert": "4-cow-vet-escalation", "plan_created": "5-wheat-variety-plan"}

def prompt_path(name):
    """Prompt file path. MEL_PROMPT_DIR points at a folder of prompt variants (any file it lacks
    falls back to the skill's references), so a sweep can compare prompt versions."""
    override = os.environ.get("MEL_PROMPT_DIR")
    if override and os.path.isfile(os.path.join(override, name)):
        return os.path.join(override, name)
    return os.path.join(WRITER, "references", name)

def load_prompts():
    text = open(prompt_path("generator_prompt.md"), encoding="utf-8").read()
    blocks = re.findall(r"```\n(.*?)```", text, flags=re.S)
    return blocks[0], blocks[1]   # system, user template

def example_text(ins):
    folder = EXAMPLE_FOR.get(ins["resolution_type"])
    if not folder:
        return "(no worked example for this resolution type)"
    d = os.path.join(WRITER, "references", "examples", folder)
    ex_ins = json.load(open(os.path.join(d, "insight.json"), encoding="utf-8"))
    ex_sv = json.load(open(os.path.join(d, f"survey.{ins['conversation_language']}.json"), encoding="utf-8"))
    return (f"insight advice: {ex_ins['advice_summary']}\nsurvey items:\n"
            + json.dumps(ex_sv["items"], ensure_ascii=False, indent=1))

def build_user(tpl, i):
    name = i["asset"]["name"]
    gloss = f"{name} = {HINDI[name]}" if i["conversation_language"] == "hi-IN" and name in HINDI else "(not needed)"
    return tpl.format(
        conversation_language=i["conversation_language"], resolution_type=i["resolution_type"],
        asset_name=name, asset_category=i["asset"]["category"], growth_stage=i["asset"].get("growth_stage"),
        concern=i["concern"], severity=i["severity"], query_summary=i["query_summary"],
        advice_summary=i["advice_summary"], action_window_days=i["action_window_days"],
        glossary_lines=gloss, scenario_examples=example_text(i))

def load_verifier_prompts():
    text = open(prompt_path("verifier_prompt.md"), encoding="utf-8").read()
    blocks = re.findall(r"```\n(.*?)```", text, flags=re.S)
    return blocks[0], blocks[1]   # system, user template

def build_verifier_user(tpl, i, items):
    return tpl.format(
        conversation_language=i["conversation_language"], resolution_type=i["resolution_type"],
        asset_name=i["asset"]["name"], concern=i["concern"], query_summary=i["query_summary"],
        advice_summary=i["advice_summary"], items_json=json.dumps(items, ensure_ascii=False, indent=1))

def fake_verifier():
    return json.dumps({"verdict": "pass", "relevance": 0.9, "checks": {}, "back_translation": [], "issues": []})

def call_verifier(system, user):
    sys.path.insert(0, ROOT)
    from agent import run_verifier
    return run_verifier(system, user)

def print_verdict(v):
    print(f"\n===== VERIFIER: {v.get('verdict', '?').upper()} (relevance {v.get('relevance')}) =====")
    failed = [k for k, ok in (v.get("checks") or {}).items() if ok is False]
    print("failed checks:", ", ".join(failed) if failed else "none")
    for x in v.get("issues") or []:
        sev = str(x.get("severity", "blocking")).upper()
        print(f"  {sev} {x.get('item_id')} [{x.get('check')}]: {x.get('problem')}\n        fix: {x.get('fix')}")
    for b in v.get("back_translation") or []:
        print(f"  {b.get('item_id')} (English): {b.get('prompt_en')}  |  options: {'; '.join(b.get('labels_en') or [])}")

def call_agent(system, user):
    """Strands agent (Bedrock model + AWS MCP tools); replaces the old direct converse call."""
    sys.path.insert(0, ROOT)
    from agent import run_agent
    return run_agent(system, user)

def fake_model(ins):
    folder = EXAMPLE_FOR.get(ins["resolution_type"], "1-mustard-aphid-spray")
    p = os.path.join(WRITER, "references", "examples", folder, f"survey.{ins['conversation_language']}.json")
    s = json.load(open(p, encoding="utf-8"))
    return json.dumps({"items": s["items"], "rationale": s.get("rationale", "dry run"),
                       "self_eval": {"schema_valid": True, "relevance_score": 0.9,
                                     "gives_new_advice": False, "asks_personal_info": False}}, ensure_ascii=False)

def load_retry_template():
    text = open(prompt_path("generator_prompt.md"), encoding="utf-8").read()
    return re.findall(r"```\n(.*?)```", text, flags=re.S)[2]   # RETRY addendum

def blocking_issues(v):
    """Issues that must be fixed. Untagged issues count as blocking, to stay safe."""
    return [x for x in (v.get("issues") or []) if str(x.get("severity", "blocking")).lower() != "minor"]

def effective_verdict(v):
    """Pass when nothing is blocking, whatever word the verifier used. 'fail' stays 'fail'."""
    if v.get("verdict") == "fail":
        return "fail"
    return "revise" if blocking_issues(v) else "pass"

def verdict_issues(v):
    """Only blocking issues go back to the generator; minor ones would just churn the wording."""
    return [f"{x.get('item_id')} [{x.get('check')}]: {x.get('problem')} Fix: {x.get('fix')}"
            for x in blocking_issues(v)]

def parse_json(raw):
    return json.loads(re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.M).strip())

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("insights"); ap.add_argument("--index", type=int); ap.add_argument("--id")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-verify", action="store_true", help="skip the verifier agent (nothing is cached)")
    ap.add_argument("--no-cache", action="store_true", help="ignore and do not write the survey cache")
    ap.add_argument("--max-retries", type=int, default=2, help="revise attempts after the first (default 2)")
    ap.add_argument("--refresh", action="store_true", help="skip the cache lookup but store the new result")
    a = ap.parse_args()
    use_cache = not (a.no_cache or a.dry_run)   # dry-run output is fake, never cache it
    rows = [json.loads(l) for l in open(a.insights, encoding="utf-8")]
    ins = next(r for r in rows if r["insight_id"] == a.id) if a.id else rows[a.index or 0]
    print("insight:", ins["insight_id"], ins["resolution_type"], ins["conversation_language"])
    d = decide(ins)
    print("decision:", json.dumps(d, ensure_ascii=False))
    if not d["survey"]:
        print("No survey, nothing to generate."); return
    if use_cache and not a.refresh:
        hit = cache.get(ins)
        if hit:
            gen = {"items": hit["items"], "rationale": hit["rationale"]}
            if hit.get("self_eval"):
                gen["self_eval"] = hit["self_eval"]
            errors, warns = check(gen, ins)   # free, code only
            print(f"CACHE HIT: reusing survey from insight {hit['source_insight_id']} "
                  f"(verifier: {hit['verifier_verdict']}). 0 model calls.")
            print(json.dumps(gen, ensure_ascii=False, indent=1))
            for x in errors: print("ERROR", x)
            for x in warns: print("WARN ", x)
            print(f"checks: {len(errors)} errors, {len(warns)} warnings")
            return
        print("CACHE MISS: generating.")
    system, tpl = load_prompts()
    base_user = build_user(tpl, ins)
    retry_tpl = load_retry_template()
    vsys, vtpl = load_verifier_prompts()
    user, gen, verdict, errors = base_user, None, None, []
    for attempt in range(a.max_retries + 1):
        print(f"\n--- attempt {attempt + 1} of {a.max_retries + 1} ---")
        raw = fake_model(ins) if a.dry_run else call_agent(system, user)
        try:
            gen = parse_json(raw)
        except json.JSONDecodeError as e:
            print("Model output was not valid JSON:", e); print(raw[:600])
            issues, prev = [f"Output was not valid JSON: {e}"], raw[:3000]
            user = base_user + "\n\n" + retry_tpl.format(issues_list="\n".join(issues), previous_items_json=prev)
            gen, verdict = None, None
            continue
        errors, warns = check(gen, ins)
        print(json.dumps(gen, ensure_ascii=False, indent=1))
        for x in errors: print("ERROR", x)
        for x in warns: print("WARN ", x)
        print(f"checks: {len(errors)} errors, {len(warns)} warnings")
        if errors:
            issues, verdict = ["Code check: " + x for x in errors], None
        elif a.no_verify:
            return
        else:
            vraw = fake_verifier() if a.dry_run else call_verifier(vsys, build_verifier_user(vtpl, ins, gen["items"]))
            try:
                verdict = parse_json(vraw)
            except json.JSONDecodeError as e:
                print("Verifier output was not valid JSON:", e); print(vraw[:600]); return
            verdict["model_verdict"], verdict["verdict"] = verdict.get("verdict"), effective_verdict(verdict)
            print_verdict(verdict)
            if verdict.get("verdict") in ("pass", "fail"):
                break
            issues = verdict_issues(verdict)
        user = base_user + "\n\n" + retry_tpl.format(
            issues_list="\n".join(issues), previous_items_json=json.dumps(gen["items"], ensure_ascii=False))
    final = (verdict or {}).get("verdict")
    print(f"\nFINAL: {final or 'no verified survey'} after {attempt + 1} attempt(s)")
    if final == "pass" and use_cache:
        cache.put(ins, gen, "pass", {"generator": os.environ.get("MODEL_ID", "global.anthropic.claude-haiku-5-5"),
                                     "verifier": os.environ.get("VERIFIER_MODEL_ID", "global.anthropic.claude-sonnet-5-5")})
        print(f"Stored in cache ({cache.stats()} surveys cached).")
    elif final != "pass":
        print("Not cached and not usable. (A fixed template fallback is not implemented yet.)")

if __name__ == "__main__":
    main()
