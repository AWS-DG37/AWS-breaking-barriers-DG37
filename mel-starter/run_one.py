#!/usr/bin/env python3
"""Run the MEL survey pipeline on ONE insight: gate -> generator (Bedrock) -> code checks.

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

HINDI = {"wheat": "गेहूं", "mustard": "सरसों", "lentil": "मसूर", "potato": "आलू", "maize": "मक्का",
         "gram": "चना", "pea": "मटर", "onion": "प्याज", "tomato": "टमाटर", "cauliflower": "फूलगोभी",
         "cabbage": "पत्तागोभी", "goat": "बकरी", "cow": "गाय", "buffalo": "भैंस"}
EXAMPLE_FOR = {"direct_action": "1-mustard-aphid-spray", "wait_and_monitor": "3-potato-blight-wait",
               "escalate_to_expert": "4-cow-vet-escalation", "plan_created": "5-wheat-variety-plan"}

def load_prompts():
    text = open(os.path.join(WRITER, "references", "generator_prompt.md"), encoding="utf-8").read()
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

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("insights"); ap.add_argument("--index", type=int); ap.add_argument("--id")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    rows = [json.loads(l) for l in open(a.insights, encoding="utf-8")]
    ins = next(r for r in rows if r["insight_id"] == a.id) if a.id else rows[a.index or 0]
    print("insight:", ins["insight_id"], ins["resolution_type"], ins["conversation_language"])
    d = decide(ins)
    print("decision:", json.dumps(d, ensure_ascii=False))
    if not d["survey"]:
        print("No survey, nothing to generate."); return
    system, tpl = load_prompts()
    user = build_user(tpl, ins)
    raw = fake_model(ins) if a.dry_run else call_agent(system, user)
    raw = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.M).strip()
    try:
        gen = json.loads(raw)
    except json.JSONDecodeError as e:
        print("Model output was not valid JSON:", e); print(raw[:600]); return
    errors, warns = check(gen, ins)
    print(json.dumps(gen, ensure_ascii=False, indent=1))
    for x in errors: print("ERROR", x)
    for x in warns: print("WARN ", x)
    print(f"checks: {len(errors)} errors, {len(warns)} warnings")

if __name__ == "__main__":
    main()
