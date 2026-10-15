# `observe-<skill>/SKILL.md` template

Replace every `<…>`; keep the sections, cut a line only when it does not apply. Add the frontmatter fields your repository requires. Paths are relative to the skills folder holding `observe-<skill>/` and `create-observe-skill/`.

````markdown
---
name: observe-<skill>
description: "Observe <skill> runs from the local Claude Code transcripts: duration, steps, where the time goes, better or worse than before, on a refreshable dashboard. Invoke on 'observe <skill>', 'why is <skill> slow', <the phrasings your users type>."
---

# Observe <Skill title>

Explains the execution time of `<skill>` with evidence: duration, steps, where each second goes, current performance against the previous window. Profile: `config.json`; engine and attribution rules: `create-observe-skill` (`scripts/observe.py`, `references/method.md`).

## 1. Scan and open the dashboard

```bash
python3 <skills>/create-observe-skill/scripts/observe.py --config <skills>/observe-<skill>/config.json serve --open
python3 <skills>/create-observe-skill/scripts/observe.py --config <skills>/observe-<skill>/config.json build
```

- History: `${XDG_STATE_HOME:-~/.local/state}/observe-<skill>/sessions/<session-id>.jsonl`; never delete it.
- Quick answer: `jq -c 'select(.run) | .run | {start, duration, spec, outcome}' ~/.local/state/observe-<skill>/sessions/*.jsonl`.

## 2. What a run is

- Starts: <`/<skill>`, `Skill` tool, subagent prompt>. Ends: <report heading | last message before the next human prompt>.
- Spec: <what makes two runs comparable>. Outcomes: <labels; which are delivered>.
- Steps: <each step and the markers that reveal it; the steps without a trace>.
- Phases: <phase → which subagents>. Causes to watch: <repository tools from `commands`>.

## 3. Better or worse than before?

1. Compare like with like: one spec at a time.
2. Pick the window (last 5 / 10 / 20 runs or 7 / 30 days): the verdict says Better or Worse only at p < 0.05, Leaning under 0.2.
3. Give both halves: median and p90 of delivered runs, delivery rate of all runs.

## 4. Explain and verify

1. Typical run: medians per step and per cause; what moved: the rows with a verdict; tail: "Lost to waiting" and the outliers in "Run spans".
2. Check a number: `observe.py --config <config> calls <transcript.jsonl> --cause "<cause>"`; check the steps of a run: `observe.py --config <config> steps <transcript.jsonl>`.
3. Trust measured spans over self-reported durations.

## 5. Known limits of this profile

- <what the profile cannot see, from the checks and the review with the skill owner>
````
