#!/usr/bin/env python3
"""Code-side checks for a generated survey, run BEFORE the verifier agent.

Input: generator output JSON {items, rationale, self_eval} (or a full survey) and the insight JSON.
Usage: python check_survey.py generated.json insight.json
Exit code 0 = no errors (warnings allowed), 1 = errors.
Errors break the schema or a hard rule. Warnings are voice or style risks.
"""
import json, os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCHEMA = os.path.join(HERE, "..", "references", "survey.schema.json")
FOLLOW_UP = {"advice_did_not_work", "crop_or_animal_damaged", "could_not_get_input", "could_not_reach_expert"}
DEVANAGARI = re.compile(r"[\u0900-\u097F]")
LATIN = re.compile(r"[A-Za-z]")
BRACKETS = re.compile(r"[()\[\]{}<>/\\|*#_~^]")
EMOJI = re.compile("[\U0001F300-\U0001FAFF\u2600-\u27BF]")
DATE = re.compile(r"\b\d{1,2}[/-]\d{1,2}([/-]\d{2,4})?\b|\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{1,2}\b|\b\d{4}-\d{2}-\d{2}\b", re.I)
PERSONAL = re.compile(r"\b(name|phone|mobile|number|address|village|aadhaar|aadhar|income|acre|hectare|bigha|land size|how much money|rupees|family)\b|नाम|फोन|मोबाइल|पता(?!\s*नही|\s*नहीं)|गाँव|गांव|आधार|आमदनी|आय |बीघा|एकड़|परिवार", re.I)

def wrap(gen, insight):
    if "survey_id" in gen:
        return gen
    c = insight["created_at"]
    return {"survey_id": "S-00000", "farmer_id": insight["farmer_id"], "source_insight_ids": [insight["insight_id"]],
            "language": insight["conversation_language"],
            "send_window": {"not_before": c, "not_after": c},
            "items": gen.get("items"), "rationale": gen.get("rationale", ""),
            **({"self_eval": gen["self_eval"]} if "self_eval" in gen else {})}

def check(gen, insight):
    errors, warns = [], []
    s = wrap(gen, insight)
    try:
        import jsonschema
        v = jsonschema.Draft202012Validator(json.load(open(SCHEMA, encoding="utf-8")))
        for e in v.iter_errors(s):
            errors.append("schema: " + "/".join(map(str, e.path)) + ": " + e.message[:160])
    except ImportError:
        warns.append("jsonschema not installed, schema not validated")
    items = s.get("items") or []
    lang = insight["conversation_language"]
    if s.get("language") != lang:
        errors.append(f"language {s.get('language')} does not match insight {lang}")
    for n, it in enumerate(items, 1):
        iid, t = it.get("item_id", "?"), it.get("type")
        if iid != f"q{n}":
            errors.append(f"{iid}: item_id should be q{n}")
        opts = it.get("options") or []
        if t == "survey-confirm" and (len(opts) != 2 or [o.get("value") for o in opts] != ["yes", "no"]):
            errors.append(f"{iid}: confirm needs exactly two options with values yes, no")
        if t == "survey-single-select" and not 2 <= len(opts) <= 4:
            errors.append(f"{iid}: single-select needs 2 to 4 options (voice-safe), has {len(opts)}")
        if t == "survey-scale" and (it.get("scale_min") != 1 or it.get("scale_max") != 5 or opts):
            errors.append(f"{iid}: scale must be 1 to 5 with no options")
        texts = [("prompt", it.get("prompt", ""))] + [("label", o.get("label", "")) for o in opts]
        for kind, txt in texts:
            if kind == "label" and len(txt) > 20:
                errors.append(f"{iid}: label over 20 characters ({len(txt)}): {txt}")
            if kind == "prompt" and len(txt) > 160:
                warns.append(f"{iid}: prompt is {len(txt)} characters, long for a spoken call")
            if EMOJI.search(txt):
                errors.append(f"{iid}: emoji in {kind}")
            if BRACKETS.search(txt):
                warns.append(f"{iid}: brackets or symbols in {kind} are read badly aloud: {txt[:40]}")
            if DATE.search(txt):
                errors.append(f"{iid}: exact date in {kind}: {txt[:40]}")
            if PERSONAL.search(txt):
                errors.append(f"{iid}: possible personal-information ask in {kind}: {txt[:40]}")
            if lang == "hi-IN" and LATIN.search(re.sub(r"[A-Za-z]+\d*", lambda m: "" if len(m.group()) <= 2 else m.group(), txt)) and kind == "label":
                warns.append(f"{iid}: Latin letters in Hindi label: {txt}")
            if lang == "hi-IN" and kind == "prompt" and not DEVANAGARI.search(txt):
                errors.append(f"{iid}: Hindi survey prompt has no Devanagari")
            if lang == "en-IN" and DEVANAGARI.search(txt):
                errors.append(f"{iid}: Devanagari in English survey ({kind})")
        for o in opts:
            if "follow_up" in o and o["follow_up"] not in FOLLOW_UP:
                errors.append(f"{iid}: unknown follow_up {o['follow_up']}")
    if items and not any("follow_up" in o for it in items for o in (it.get("options") or [])) \
            and not any("follow_up_at_or_below" in it for it in items):
        errors.append("no answer carries a follow_up reason, so bad outcomes cannot be routed back")
    se = s.get("self_eval") or {}
    if se.get("gives_new_advice") or se.get("asks_personal_info"):
        errors.append("self_eval admits new advice or a personal question")
    return errors, warns

if __name__ == "__main__":
    gen, ins = (json.load(open(p, encoding="utf-8")) for p in sys.argv[1:3])
    e, w = check(gen, ins)
    for x in e: print("ERROR", x)
    for x in w: print("WARN ", x)
    print(f"{len(e)} errors, {len(w)} warnings")
    sys.exit(1 if e else 0)
