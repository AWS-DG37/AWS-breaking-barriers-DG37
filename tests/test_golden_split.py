"""The checked-in dev/test split must be reproducible, leak-free and match the prompts."""

import json
import re
from pathlib import Path

from mel.evaluation import split as partition
from mel.evaluation.policy import load_split

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "eval/prompt_example_split.json"
SOURCE = ROOT / partition.SOURCE


def _jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_the_checked_in_manifest_is_what_the_seeded_builder_produces():
    assert partition.build(SOURCE.read_bytes()) == json.loads(MANIFEST.read_text())


def test_halves_partition_the_source_without_shared_advice_farmers_or_conversations():
    manifest, source, _ = load_split(MANIFEST)
    dev, test = manifest["dev_ids"], manifest["holdout_ids"]
    assert len(dev) + len(test) == len(source) == 150
    assert partition.leaks(source, dev, test) == []
    assert abs(len(dev) - len(test)) <= 10
    surveys = manifest["counts"]["dev"]["survey"], manifest["counts"]["test"]["survey"]
    assert sum(surveys) == 95 and abs(surveys[0] - surveys[1]) <= 5


def test_jsonl_halves_are_the_exact_source_rows():
    manifest, source, _ = load_split(MANIFEST)
    for path, ids in (
        (ROOT / "eval/golden_holdout.jsonl", manifest["holdout_ids"]),
        (ROOT / "eval/golden_dev.jsonl", manifest["dev_ids"]),
    ):
        assert _jsonl(path) == [source[key] for key in ids]


def test_prompts_embed_exactly_the_dev_prompt_examples():
    manifest, source, _ = load_split(MANIFEST)
    assert len(manifest["prompt_example_ids"]) == 7
    assert set(manifest["prompt_example_ids"]) <= set(manifest["dev_ids"])
    for name in ("generate.txt", "review.txt"):
        text = (ROOT / "prompts" / name).read_text()
        embedded = re.findall(r"^Reference (G-\d{4}) \| (\w+) \| (\w+)\nAdvice context: (.+)$",
                              text, re.MULTILINE)
        assert [e[0] for e in embedded] == manifest["prompt_example_ids"], name
        for golden_id, resolution, asset, advice in embedded:
            insight = source[golden_id]["insight"]
            assert (resolution, asset, advice) == (
                insight["resolution_type"], insight["asset"]["name"], insight["advice_summary"],
            ), golden_id


def test_swaps_keep_the_resolution_type_and_record_the_replaced_example():
    manifest, source, _ = load_split(MANIFEST)
    for swap in manifest["prompt_example_swaps"]:
        assert swap["replaced"] in manifest["holdout_ids"]
        assert swap["replacement"] in manifest["prompt_example_ids"]
        for key in ("replaced", "replacement"):
            assert source[swap[key]]["insight"]["resolution_type"] == swap["resolution_type"]


def test_rows_used_for_tuning_sit_in_dev_and_the_verifier_enforces_it(tmp_path):
    manifest, _, _ = load_split(MANIFEST)
    exposed = {x["golden_id"] for x in manifest["tuning_exposures"]}
    assert exposed == set(partition.TUNING_EXPOSURES)
    assert exposed <= set(manifest["dev_ids"])
    moved = dict(manifest, tuning_exposures=[{"golden_id": manifest["holdout_ids"][0]}])
    copy = tmp_path / "eval" / "split.json"
    copy.parent.mkdir()
    copy.write_text(json.dumps(moved))
    (tmp_path / partition.SOURCE).parent.mkdir(parents=True)
    (tmp_path / partition.SOURCE).write_bytes(SOURCE.read_bytes())
    try:
        load_split(copy)
    except ValueError as error:
        assert "tuning" in str(error)
    else:
        raise AssertionError("a tuning exposure in test was accepted")


def test_grouping_joins_rows_by_any_shared_key():
    def row(gid, farmer, conversation, advice, asset):
        return gid, {"golden_id": gid, "insight": {
            "farmer_id": farmer, "conversation_id": conversation, "advice_summary": advice,
            "asset": {"name": asset},
        }}

    rows = dict([
        row("G-1", "F-1", "C-1", "Spray the potatoes.", "potato"),
        row("G-2", "F-2", "C-2", "spray the  wheat.", "wheat"),  # Same normalized advice.
        row("G-3", "F-2", "C-3", "Call a vet.", "goat"),  # Same farmer as G-2.
        row("G-4", "F-4", "C-4", "Call a doctor.", "cow"),
    ])
    assert partition.groups(rows) == [["G-1", "G-2", "G-3"], ["G-4"]]
