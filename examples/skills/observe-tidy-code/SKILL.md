---
name: observe-tidy-code
description: "Observe tidy-code runs from the local Claude Code transcripts: duration, steps, where the time goes, better or worse than before, on a refreshable dashboard. Invoke on 'observe tidy-code', 'why is tidy-code slow'."
---

# Observe Tidy Code

Explains the execution time of `tidy-code` with evidence: duration, steps, where each second goes, current performance against the previous window. Profile: `config.json`; engine and attribution rules: `create-observe-skill` (`scripts/observe.py`, `references/method.md`).

## 1. Scan and open the dashboard

```bash
python3 create-observe-skill/scripts/observe.py --config observe-tidy-code/config.json serve --open
python3 create-observe-skill/scripts/observe.py --config observe-tidy-code/config.json build
```

- History: `${XDG_STATE_HOME:-~/.local/state}/observe-tidy-code/sessions/<session-id>.jsonl`; never delete it.
- Quick answer: `jq -c 'select(.run) | .run | {start, duration, spec, outcome}' ~/.local/state/observe-tidy-code/sessions/*.jsonl`.

## 2. What a run is

- Starts: `/tidy-code first <N> files`. Ends: the `## Tidy report` heading; without one, the last message before the next human prompt.
- Spec: the number of files (`{} file(s)`). Outcomes: PR OPENED (delivered), ABORTED, ASKED HUMAN, NO REPORT.
- Steps: 1 — Pick the files (triage subagent, `picking-rules.md`), 2 — Clean (`lintkit --fix`), 3 — Review and PR (review subagent, `gh pr create`).
- Phases: triage → the triage subagent; review → the review subagent. Causes to watch: `lint` (`lintkit`), build & test (`pytest`).

## 3. Better or worse than before?

1. Compare like with like: one spec at a time.
2. Pick the window (last 5 / 10 / 20 runs or 7 / 30 days): the verdict says Better or Worse only at p < 0.05, Leaning under 0.2.
3. Give both halves: median and p90 of delivered runs, delivery rate of all runs.

## 4. Explain and verify

1. Typical run: medians per step and per cause; what moved: the rows with a verdict; tail: "Lost to waiting" and the outliers in "Run spans".
2. Check a number: `observe.py --config <config> calls <transcript.jsonl> --cause "build & test"`; check the steps of a run: `observe.py --config <config> steps <transcript.jsonl>`.
3. Trust measured spans over self-reported durations.

## 5. Known limits of this profile

- A run that asks a question before the pull request ends at that question: the time spent answering it is not part of the run.
