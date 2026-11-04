"""English -> target-language survey translation, with a translation cache.

The translate pipeline is:  generator writes English -> translate() -> code checks -> verifier.

Safety rule (from docs/architecture.md): ids, types, option values and follow_up codes must not
change in translation. So the translator only supplies text; merge() copies the English survey and
swaps in the translated prompt/label strings. A translation that adds, drops or renames anything is
rejected instead of merged.

Cache: one entry per (translator model, target language, exact English text of the survey) in
cache/translations/<sha1>.json. A hit costs no model call. Env MEL_CACHE_DIR moves the cache root.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROMPT = ROOT / "skills" / "mel-survey-writer" / "references" / "translator_prompt.md"
CACHE_DIR = Path(os.environ.get("MEL_CACHE_DIR", ROOT / "cache")) / "translations"


def load_prompts():
    """Return (system, user_template, retry_template) from translator_prompt.md."""
    blocks = re.findall(r"```\n(.*?)```", PROMPT.read_text(encoding="utf-8"), flags=re.S)
    return blocks[0], blocks[1], blocks[2]


def text_only(items):
    """The parts of a survey the translator sees: ids, prompts, option values and labels."""
    return [{"item_id": it.get("item_id"), "prompt": it.get("prompt", ""),
             "options": [{"value": o.get("value"), "label": o.get("label", "")}
                         for o in (it.get("options") or [])]}
            for it in items]


def _key(model_id, target_language, english_items):
    raw = json.dumps([model_id, target_language, text_only(english_items)], ensure_ascii=False,
                     sort_keys=True)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def cache_get(model_id, target_language, english_items):
    try:
        entry = json.loads((CACHE_DIR / f"{_key(model_id, target_language, english_items)}.json")
                           .read_text(encoding="utf-8"))
        return entry["items"]
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        return None


def cache_put(model_id, target_language, english_items, translated_items):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / f"{_key(model_id, target_language, english_items)}.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"translator": model_id, "target_language": target_language,
                               "english": text_only(english_items), "items": translated_items},
                              ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def parse_loose(raw):
    """Parse a JSON object even if the model wrapped it in fences or chatted around it."""
    s = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.M).strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        start, end = s.find("{"), s.rfind("}")
        if start == -1 or end <= start:
            raise
        return json.loads(s[start:end + 1])


def merge(english_items, translated):
    """Copy the English items and replace only prompt/label text. Returns (items, problems)."""
    by_id = {t.get("item_id"): t for t in (translated.get("items") or [])}
    problems, out = [], []
    if len(by_id) != len(english_items):
        problems.append(f"translation has {len(by_id)} items, English has {len(english_items)}")
    for it in english_items:
        new = copy.deepcopy(it)
        t = by_id.get(it.get("item_id"))
        if t is None:
            problems.append(f"{it.get('item_id')}: missing from translation")
            out.append(new)
            continue
        if not str(t.get("prompt", "")).strip():
            problems.append(f"{it.get('item_id')}: empty translated prompt")
        else:
            new["prompt"] = t["prompt"].strip()
        t_opts = {o.get("value"): o for o in (t.get("options") or [])}
        for o in new.get("options") or []:
            to = t_opts.get(o.get("value"))
            if to is None or not str(to.get("label", "")).strip():
                problems.append(f"{it.get('item_id')}: option value '{o.get('value')}' "
                                "missing or unlabeled in translation (keep option values unchanged)")
            else:
                o["label"] = to["label"].strip()
        extra = set(t_opts) - {o.get("value") for o in new.get("options") or []}
        if extra:
            problems.append(f"{it.get('item_id')}: translation added or renamed options {sorted(extra)}")
        out.append(new)
    return out, problems
