"""Survey skeleton cache.

One verified survey is stored per key and reused for every farmer whose insight has the same key:
    (advice_summary, resolution_type, crop or animal name, concern, conversation_language)

Only surveys that passed the code checks AND got a verifier verdict of "pass" are stored, so a cache hit
is always a survey that was already checked. The survey text never contains farmer data (the prompt
forbids it), so it is safe to share across farmers.

Files live in ./cache/<sha1>.json. Delete the folder to clear the cache.
Env: MEL_CACHE_DIR to change the location.
"""
import hashlib, json, os, time

DIR = os.environ.get("MEL_CACHE_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache"))


def key_of(ins):
    return (ins["advice_summary"], ins["resolution_type"], ins["asset"]["name"],
            ins["concern"], ins["conversation_language"])


def _path(key):
    digest = hashlib.sha1(json.dumps(key, ensure_ascii=False).encode("utf-8")).hexdigest()
    return os.path.join(DIR, digest + ".json")


def get(ins):
    """Return {items, rationale, self_eval, ...} or None."""
    try:
        with open(_path(key_of(ins)), encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def put(ins, gen, verdict, models):
    os.makedirs(DIR, exist_ok=True)
    entry = {"key": list(key_of(ins)), "items": gen["items"], "rationale": gen.get("rationale", ""),
             "self_eval": gen.get("self_eval"), "verifier_verdict": verdict,
             "models": models, "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
             "source_insight_id": ins["insight_id"]}
    tmp = _path(key_of(ins)) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(entry, f, ensure_ascii=False, indent=1)
    os.replace(tmp, _path(key_of(ins)))   # atomic, no half-written files


def stats():
    n = len([x for x in os.listdir(DIR) if x.endswith(".json")]) if os.path.isdir(DIR) else 0
    return n
