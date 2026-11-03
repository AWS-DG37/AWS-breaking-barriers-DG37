"""Build docs/survey-question-patterns.html from golden_set.jsonl and the worked scenarios.

Run from the repo root, and again whenever Digital Green updates the pack:

    python3 scripts/build_question_patterns.py

Optional arguments: <pack_dir> <out.html>. Standard library only.
"""
import collections
import html
import json
import sys
from pathlib import Path

PACK = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("docs/participant-pack")
OUT = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("docs/survey-question-patterns.html")
E = html.escape
ORDER = ["direct_action", "plan_created", "wait_and_monitor", "do_not_act", "escalate_to_expert"]
LABEL = {"direct_action": "Direct action", "plan_created": "Plan created", "wait_and_monitor": "Wait and monitor",
         "do_not_act": "Do not act", "escalate_to_expert": "Escalate to expert"}

gold = [json.loads(l) for l in (PACK / "data/golden_set.jsonl").read_text().splitlines() if l.strip()]
rules = json.loads((PACK / "json/rules/rules.json").read_text())
insights = [json.loads(l) for l in (PACK / "data/insights.jsonl").read_text().splitlines() if l.strip()]
surveys = [g for g in gold if g["expected"]["survey"]]


def slot(text):
    """Return (template, fill) for questions that carry a slot, else (text, None)."""
    for pre, suf, name in (("How is your ", " now?", "crop or animal"), ("Did you ", "?", "advised action"),
                           ("Have you seen ", "?", "warning sign")):
        if text.startswith(pre) and text.endswith(suf):
            fill = text[len(pre):-len(suf)]
            if name == "advised action" and fill in ("hold off as advised", "meet the veterinary doctor"):
                return text, None
            return f"{pre}{{{name}}}{suf}", fill
    return text, None


# per resolution -> position -> template -> stats
pat = {r: collections.OrderedDict() for r in ORDER}
per_res = collections.Counter()
item_counts = collections.defaultdict(collections.Counter)
answer_sets = collections.defaultdict(lambda: {"n": 0, "templates": set()})
texts, n_items = set(), 0
for g in surveys:
    r = g["insight"]["resolution_type"]
    per_res[r] += 1
    item_counts[r][len(g["expected"]["items"])] += 1
    for pos, it in enumerate(g["expected"]["items"], 1):
        n_items += 1
        texts.add(it["text"])
        tmpl, fill = slot(it["text"])
        opts = tuple(it["options"])
        key = (pos, tmpl)
        e = pat[r].setdefault(key, {"n": 0, "opts": collections.Counter(), "show_if": collections.Counter(),
                                    "fills": collections.Counter(), "examples": {}, "literal": 0})
        e["n"] += 1
        e["opts"][opts] += 1
        if "show_if" in it:
            e["show_if"][it["show_if"]] += 1
        if fill:
            e["fills"][fill] += 1
            src_text = g["insight"]["asset"]["name"] if "crop or animal" in tmpl else g["insight"]["advice_summary"]
            e["literal"] += fill.lower() in (src_text or "").lower()
            e["examples"].setdefault(fill, (g["golden_id"], g["insight"]["advice_summary"], g["insight"]["asset"]["name"]))
        a = answer_sets[opts]
        a["n"] += 1
        a["templates"].add(tmpl)

assert all(len(e["opts"]) == 1 for r in ORDER for e in pat[r].values()), "a question has two answer sets"

# escalate_to_expert by asset category, golden vs input
esc_gold = collections.Counter(g["insight"]["asset"]["category"] for g in surveys if g["insight"]["resolution_type"] == "escalate_to_expert")
esc_input = collections.Counter(i["asset"]["category"] for i in insights
                                if i.get("quality_flag") == "clean" and i.get("resolution_type") == "escalate_to_expert")

# worked scenarios
scen = []
for d in sorted((PACK / "json/scenarios").iterdir()):
    f = d / "survey.en-IN.json"
    if not f.exists():
        continue
    ins = json.loads((d / "insight.json").read_text())
    scen.append((d.name, ins["resolution_type"], json.loads(f.read_text())["items"]))

n_follow_golden = sum(1 for g in surveys for it in g["expected"]["items"] if any(k in it for k in ("follow_up", "follow_up_reason")))

# ---------- render ----------
CSS = (Path(__file__).parent / "question_patterns.css").read_text()
out = [f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Survey Question Patterns</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Archivo:wght@600;700&family=Source+Sans+3:wght@400;600&family=JetBrains+Mono:wght@400;500&display=swap">
<style>{CSS}</style>
</head>
<body>
<div class="wrap">
<header class="col">
  <div class="eyebrow">Digital Green · MEL Survey Agent · working notes</div>
  <h1>Survey Question Patterns</h1>
  <p class="lede">The 95 surveys in <code>golden_set.jsonl</code> (version 1) follow one fixed template per resolution type. No Digital Green document states these templates. This page reads them out of the data.</p>
  <p class="note">The Vision document defines the golden dataset as "insights with expected high-quality surveys, used for evaluation", so these templates are what the evaluation compares against. <strong>They are still evidence, not a spec.</strong> The brief's own worked scenarios use different questions (see the comparison below). Confirm with Digital Green before the team treats this as a question library.</p>
</header>
<div class="stats" role="list">
  <div role="listitem"><b>{len(surveys)}</b><span>surveys</span></div>
  <div role="listitem"><b>{n_items}</b><span>questions</span></div>
  <div role="listitem"><b>{len(texts)}</b><span>distinct texts</span></div>
  <div role="listitem"><b>{len(answer_sets)}</b><span>answer sets</span></div>
  <div role="listitem"><b>{len(ORDER)}</b><span>templates</span></div>
</div>
<nav class="toc col" aria-label="Resolution types">"""]
out.append(" ".join(f'<a href="#{r}">{LABEL[r]} <span>{per_res[r]}</span></a>' for r in ORDER))
out.append("</nav>")

for r in ORDER:
    sizes = ", ".join(f"{c} with {k} questions" for k, c in sorted(item_counts[r].items()))
    out.append(f'<section class="res" id="{r}"><div class="res-head"><h2>{LABEL[r]}</h2>'
               f'<span class="mono">{r}</span><span class="count">{per_res[r]} surveys · {sizes}</span></div>'
               f'<p class="asks"><span class="k">rules.json asks</span> {E(rules["survey_by_resolution"][r]["asks"])}</p>'
               '<div class="tbl"><table><thead><tr><th>#</th><th>Question</th><th>Answers</th><th class="num">Used</th></tr></thead><tbody>')
    for (pos, tmpl), e in sorted(pat[r].items(), key=lambda kv: (kv[0][0], -kv[1]["n"])):
        opts = next(iter(e["opts"]))
        cond = "".join(f'<div class="cond">only if <code>{E(k)}</code> ({c} of {e["n"]})</div>' for k, c in e["show_if"].items())
        t = E(tmpl).replace("{", '<span class="slot">{').replace("}", "}</span>")
        chips = "".join(f'<span class="chip">{E(o)}</span>' for o in opts)
        out.append(f'<tr><td class="pos">q{pos}</td><td>{t}{cond}</td><td><div class="chips">{chips}</div></td>'
                   f'<td class="num">{e["n"]}</td></tr>')
    out.append("</tbody></table></div>")
    for (pos, tmpl), e in pat[r].items():
        if not e["fills"]:
            continue
        src = "asset.name" if "crop or animal" in tmpl else "advice_summary"
        n_fill = sum(e["fills"].values())
        how = (f"copied from <code>{src}</code>" if e["literal"] == n_fill else
               f"paraphrased from <code>{src}</code>, {e['literal']} of {n_fill} word for word")
        out.append(f'<details><summary>q{pos} slot fills: {len(e["fills"])} values, {how}</summary>'
                   '<div class="tbl"><table class="fills"><thead><tr><th>Fill</th><th class="num">Used</th><th>Example source</th></tr></thead><tbody>')
        for fill, c in e["fills"].most_common():
            gid, adv, asset = e["examples"][fill]
            ex = E(asset or "") if src == "asset.name" else E(adv)
            out.append(f'<tr><td>{E(fill)}</td><td class="num">{c}</td><td><span class="mono gid">{gid}</span> {ex}</td></tr>')
        out.append("</tbody></table></div></details>")
    if r == "escalate_to_expert":
        out.append(f'<p class="small">Every golden expert survey is about livestock ({dict(esc_gold)}), so the question always says veterinary doctor. '
                   f'The full input matches: clean <code>escalate_to_expert</code> insights split as {dict(esc_input)}. A crop case would need different wording, but the practice data has none.</p>')
    out.append("</section>")

# answer sets
out.append('<section class="col"><h2>The answer sets</h2><p>Each question text always comes with the same answers. These are all of them.</p>'
           '<div class="tbl"><table><thead><tr><th>Answers</th><th class="num">Used</th><th>Questions</th></tr></thead><tbody>')
for opts, a in sorted(answer_sets.items(), key=lambda kv: -kv[1]["n"]):
    chips = "".join(f'<span class="chip">{E(o)}</span>' for o in opts)
    qs = "<br>".join(E(t) for t in sorted(a["templates"])[:4]) + (" …" if len(a["templates"]) > 4 else "")
    out.append(f'<tr><td><div class="chips">{chips}</div></td><td class="num">{a["n"]}</td><td class="small">{qs}</td></tr>')
out.append("</tbody></table></div></section>")

# scenarios
out.append('<section class="col"><h2>Compare: the brief\'s worked scenarios</h2>'
           '<p>The five worked surveys use other wording, a scale question, and mark the bad answers with a <code>follow_up</code> reason. '
           f'The golden surveys mark none ({n_follow_golden} of {n_items} questions carry a follow-up field).</p>')
for name, r, items in scen:
    out.append(f'<div class="scen"><div class="scen-head"><b>{E(name)}</b> <span class="mono">{r}</span></div><ol>')
    for it in items:
        if it["type"] == "survey-scale":
            ans = (f'<span class="chip">{it["scale_min"]} to {it["scale_max"]}</span>'
                   f'<span class="fu">follow-up at or below {it["follow_up_at_or_below"]}: {E(it["follow_up_reason"])}</span>')
        else:
            ans = "".join(f'<span class="chip{" bad" if o.get("follow_up") else ""}" title="{E(o.get("follow_up") or "")}">{E(o["label"])}</span>'
                          for o in it["options"])
        out.append(f'<li><span class="mono t">{it["type"].replace("survey-", "")}</span> {E(it["prompt"])}<div class="chips">{ans}</div></li>')
    out.append("</ol></div>")
out.append('<p class="small"><span class="chip bad">Answer</span> marks an answer that carries a <code>follow_up</code> reason and goes back to FarmerChat for help.</p></section>')

out.append("""<section class="col"><h2>Check before use</h2><ul>
<li><strong>No follow-up flags.</strong> The brief requires a <code>follow_up</code> reason on bad answers, for example "more insects than before" or "the cow is worse". The golden answers "Worse" and "Could not reach" carry none.</li>
<li><strong><code>show_if</code> is not in the schema.</strong> The golden set shows "If not, what stopped you?" only when q1 is not "Yes". <code>survey.schema.json</code> has no field for a condition.</li>
<li><strong>English only.</strong> All golden questions are English. The farmer gets the copy in the insight's <code>conversation_language</code>, mostly hi-IN, and Hindi answer labels must still fit 20 characters.</li>
<li><strong>Different shape.</strong> Golden items use <code>id</code>, <code>text</code> and plain option strings. <code>survey.schema.json</code> uses <code>item_id</code>, <code>type</code>, <code>prompt</code> and option objects with <code>value</code> and <code>label</code>.</li>
<li><strong>Version 1.</strong> Digital Green can update the golden set during the event. Rebuild this page when it changes.</li>
</ul></section>
<footer class="col">Built from the Digital Green participant pack, version 1: <code>data/golden_set.jsonl</code>, <code>json/rules/rules.json</code>, <code>json/scenarios/*/survey.en-IN.json</code>. Synthetic data. Every count on this page is computed from those files by <code>scripts/build_question_patterns.py</code>. See also the <a href="system-diagram.html">system diagram</a>.</footer>
</div>
</body>
</html>""")
OUT.write_text("\n".join(out))
print("wrote", OUT, "| surveys", len(surveys), "items", n_items, "texts", len(texts), "answer sets", len(answer_sets),
      "| esc gold", dict(esc_gold), "esc input", dict(esc_input), "| golden follow-up fields", n_follow_golden)
