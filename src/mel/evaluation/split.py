"""Grouped, stratified dev/test split of Digital Green's golden set.

The 150 golden rows repeat advice texts, farmers and conversations, so a random row
split leaks. Rows that share a normalized advice text, a farmer or a conversation
form one group, and a whole group goes to one half. Groups are assigned to balance
expected rule, resolution type, conversation language and their joint stratum.

Rebuild with `python -m mel.evaluation.split` from the repository root. The build is
deterministic: the seed and trial count are recorded in the manifest.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
from collections import Counter
from collections.abc import Iterable, Mapping
from pathlib import Path

VERSION = "golden-dev-test-v2"
SOURCE = "docs/participant-pack/data/golden_set.jsonl"
SEED = 37
TRIALS = 2000
# The v3 prompts embedded these seven examples before the dev/test split existed.
# A swap replaces any of them that the split puts in test.
ORIGINAL_PROMPT_EXAMPLES = (
    "G-0131", "G-0032", "G-0019", "G-0003", "G-0045", "G-0004", "G-0018",
)
# Test rows whose results were used to tune a prompt or a judge. After assignment,
# each one moves to dev with its whole group, so test stays untouched by tuning.
TUNING_EXPOSURES = {
    "G-0073": "its test-half judgment prompted relevance rubric v4 (the Hindi "
               "'Do you need more help?' question)",
}
GROUPING = ("normalized_advice", "farmer_id", "conversation_id")
STRATA = ("rule", "resolution_type", "conversation_language")


def asset_names(rows: Iterable[Mapping]) -> list[str]:
    names = {row["insight"]["asset"].get("name") for row in rows}
    return sorted((n.lower() for n in names if n), key=len, reverse=True)


def normalize_advice(text: str, names: list[str]) -> str:
    """Lowercase, collapse whitespace, and replace crop/animal names (and plurals)."""
    text = " ".join(text.lower().split())
    if not names:
        return text
    pattern = r"\b(?:" + "|".join(map(re.escape, names)) + r")(?:e?s)?\b"
    return re.sub(pattern, "<asset>", text)


def group_keys(row: Mapping, names: list[str]) -> tuple[str, str, str]:
    insight = row["insight"]
    return (
        "advice:" + normalize_advice(insight["advice_summary"], names),
        "farmer:" + insight["farmer_id"],
        "conversation:" + insight["conversation_id"],
    )


def stratum(row: Mapping) -> tuple[str, str, str]:
    insight = row["insight"]
    return (
        row["expected"]["rule"], insight["resolution_type"], insight["conversation_language"],
    )


def groups(rows: Mapping[str, Mapping]) -> list[list[str]]:
    """Connected components of rows that share any grouping key, sorted by first ID."""
    names = asset_names(rows.values())
    parent: dict[str, str] = {}

    def find(node: str) -> str:
        parent.setdefault(node, node)
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    for golden_id, row in rows.items():
        for key in group_keys(row, names):
            parent[find(golden_id)] = find(key)
    members: dict[str, list[str]] = {}
    for golden_id in sorted(rows):
        members.setdefault(find(golden_id), []).append(golden_id)
    return sorted(members.values(), key=lambda group: group[0])


def _features(row: Mapping) -> list[tuple]:
    rule, resolution, language = stratum(row)
    return [
        ("rows",), ("survey", row["expected"]["survey"]), ("rule", rule),
        ("resolution_type", resolution), ("language", language),
        ("stratum", rule, resolution, language),
    ]


def _imbalance(dev: Counter, test: Counter) -> int:
    return sum((dev[key] - test[key]) ** 2 for key in dev.keys() | test.keys())


def assign(rows: Mapping[str, Mapping], seed: int = SEED, trials: int = TRIALS) -> dict[str, str]:
    """Map each golden ID to "dev" or "test".

    Each trial shuffles the groups, places the largest first, and puts each group in
    the half that leaves the smaller squared imbalance across all strata. The trial
    with the lowest final imbalance wins.
    """
    rng = random.Random(seed)
    components = groups(rows)
    features = {
        tuple(group): Counter(f for gid in group for f in _features(rows[gid]))
        for group in components
    }
    best: tuple[int, dict[str, str]] | None = None
    for _ in range(trials):
        order = components[:]
        rng.shuffle(order)
        order.sort(key=len, reverse=True)
        halves = {"dev": Counter(), "test": Counter()}
        sides: dict[str, str] = {}
        for group in order:
            add = features[tuple(group)]
            to_dev = _imbalance(halves["dev"] + add, halves["test"])
            to_test = _imbalance(halves["dev"], halves["test"] + add)
            side = "dev" if (to_dev, rng.random()) < (to_test, 0.5) else "test"
            halves[side] += add
            sides.update(dict.fromkeys(group, side))
        cost = _imbalance(halves["dev"], halves["test"])
        if best is None or cost < best[0]:
            best = (cost, sides)
    assert best is not None
    return best[1]


def counts(rows: Iterable[Mapping]) -> dict:
    rows = list(rows)

    def tally(values: Iterable) -> dict[str, int]:
        return dict(sorted(Counter(values).items()))

    return {
        "rows": len(rows),
        "survey": sum(row["expected"]["survey"] for row in rows),
        "rule": tally(row["expected"]["rule"] for row in rows),
        "resolution_type": tally(row["insight"]["resolution_type"] for row in rows),
        "conversation_language": tally(
            row["insight"]["conversation_language"] for row in rows
        ),
    }


def leaks(rows: Mapping[str, Mapping], dev: Iterable[str], test: Iterable[str]) -> list[str]:
    """Grouping keys found in both halves. An empty list means the halves are disjoint."""
    names = asset_names(rows.values())

    def keys(ids: Iterable[str]) -> set[str]:
        return {key for gid in ids for key in group_keys(rows[gid], names)}

    return sorted(keys(dev) & keys(test))


def _swap(rows: Mapping[str, Mapping], sides: Mapping[str, str], example: str,
          taken: set[str]) -> str:
    """The first dev row with the example's resolution type and survey decision,
    preferring the same expected rule and a crop/animal no other example uses."""
    wanted = rows[example]
    used_assets = {rows[gid]["insight"]["asset"].get("name") for gid in taken}
    candidates = [
        gid for gid in sorted(rows)
        if sides[gid] == "dev" and gid not in taken
        and rows[gid]["insight"]["resolution_type"] == wanted["insight"]["resolution_type"]
        and rows[gid]["expected"]["survey"] == wanted["expected"]["survey"]
    ]
    if not candidates:
        raise ValueError(f"no dev replacement for prompt example {example}")
    return min(candidates, key=lambda gid: (
        rows[gid]["expected"]["rule"] != wanted["expected"]["rule"],
        rows[gid]["insight"]["asset"].get("name") in used_assets,
        gid,
    ))


def read_rows(body: bytes) -> dict[str, dict]:
    rows = [json.loads(line) for line in body.decode("utf-8-sig").splitlines() if line.strip()]
    return {row["golden_id"]: row for row in rows}


def build(source_bytes: bytes, seed: int = SEED, trials: int = TRIALS) -> dict:
    rows = read_rows(source_bytes)
    sides = assign(rows, seed, trials)
    exposures = []
    for exposed, reason in sorted(TUNING_EXPOSURES.items()):
        group = next(g for g in groups(rows) if exposed in g)
        moved = [gid for gid in group if sides[gid] == "test"]
        sides.update(dict.fromkeys(group, "dev"))
        exposures.append({"golden_id": exposed, "moved_to_dev": moved, "reason": reason})
    examples, swaps = [], []
    for example in ORIGINAL_PROMPT_EXAMPLES:
        if sides[example] == "dev":
            examples.append(example)
            continue
        replacement = _swap(rows, sides, example, set(examples) | set(ORIGINAL_PROMPT_EXAMPLES))
        examples.append(replacement)
        swaps.append({
            "replaced": example, "replacement": replacement,
            "resolution_type": rows[example]["insight"]["resolution_type"],
            "reason": "original prompt example fell in the test half",
        })
    dev = [gid for gid in sorted(rows) if sides[gid] == "dev"]
    test = [gid for gid in sorted(rows) if sides[gid] == "test"]
    components = groups(rows)
    return {
        "version": VERSION,
        "source": SOURCE,
        "source_sha256": hashlib.sha256(source_bytes).hexdigest(),
        "method": {
            "seed": seed,
            "trials": trials,
            "grouping": list(GROUPING),
            "advice_normalization": (
                "lowercase, collapse whitespace, replace source crop/animal names and plurals"
            ),
            "strata": list(STRATA),
            "groups": len(components),
            "largest_group": max(map(len, components)),
            "builder": "python -m mel.evaluation.split",
        },
        "prompt_example_ids": examples,
        "prompt_example_insight_ids": [rows[gid]["insight"]["insight_id"] for gid in examples],
        "prompt_example_swaps": swaps,
        "tuning_exposures": exposures,
        "dev_ids": dev,
        "holdout_ids": test,
        "counts": {
            "dev": counts(rows[gid] for gid in dev),
            "test": counts(rows[gid] for gid in test),
        },
        "notes": [
            "Dev holds the prompt examples and is the only half used to tune prompts or "
            "calibrate judges. Test (holdout_ids) is scored, never tuned on.",
            "The decision rules were written while reading all 150 rows, so rules scores "
            "on either half are a regression baseline, not a generalization estimate.",
            "Golden reference questions are English; 141 of 150 conversations are Hindi.",
            "Grouping catches exact normalized advice duplicates, not paraphrases.",
        ],
    }


def _jsonl(rows: Mapping[str, Mapping], ids: list[str]) -> str:
    return "".join(json.dumps(rows[gid], ensure_ascii=False) + "\n" for gid in ids)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("."))
    args = parser.parse_args()
    body = (args.root / SOURCE).read_bytes()
    manifest = build(body)
    rows = read_rows(body)
    eval_dir = args.root / "eval"
    (eval_dir / "prompt_example_split.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (eval_dir / "golden_holdout.jsonl").write_text(_jsonl(rows, manifest["holdout_ids"]))
    (eval_dir / "golden_dev.jsonl").write_text(_jsonl(rows, manifest["dev_ids"]))
    print(json.dumps({
        "dev": manifest["counts"]["dev"]["rows"], "test": manifest["counts"]["test"]["rows"],
        "swaps": manifest["prompt_example_swaps"],
    }))


if __name__ == "__main__":
    main()
