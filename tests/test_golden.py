"""Score the rules.json survey decision against data/pack/data/golden_set.jsonl."""
from dg_agent.checks import decide
from dg_agent.models import PACK
from conftest import jsonl

MIN_ACCURACY = 0.85  # raise as the rules improve


def score(**kw):
    gold = jsonl(PACK / "data" / "golden_set.jsonl")
    misses = [(g["golden_id"], g["expected"].get("rule"), g["expected"].get("reason_code"), decide(g["insight"], **kw)[1])
              for g in gold if decide(g["insight"], **kw)[0] != g["expected"]["survey"]]
    return 1 - len(misses) / len(gold), misses


def test_golden_accuracy(capsys):
    acc, misses = score()
    base, _ = score(use_verification=False, use_season_end=False)
    with capsys.disabled():
        print(f"\ngolden accuracy {acc:.1%} (rules.json resolution+junk only: {base:.1%}); misses: {misses}")
    assert acc >= MIN_ACCURACY
