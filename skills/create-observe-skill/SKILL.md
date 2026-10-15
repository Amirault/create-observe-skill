---
name: create-observe-skill
description: "Create an observe-<skill> skill that measures another skill's runs from the local Claude Code transcripts: duration, steps, where the time goes, better or worse than before. Invoke on 'create observe-<skill>', 'observe my skill', 'crée un skill observe pour <skill>'."
effort: medium
---

# Create Observe Skill

Builds `observe-<skill>/` next to the observed skill, in the same skills folder: a `config.json` profile and a `SKILL.md`. The engine is in this skill's `scripts/` (`observe.py`, `dashboard.html`); generated skills call it by its path relative to the skills folder. The profile comes from evidence in real transcripts, never from the observed skill's description alone. Paths below are relative to this skill's folder; `<skills>` is the folder holding it.

## 1. Read the skill, then survey its runs

1. Read `<skills>/<skill>/SKILL.md`: how it is invoked, its steps, the subagents it launches, how it ends (report, pull request, question), the outcome words it writes.
2. Collect the evidence (former names come from the git history of that `SKILL.md`):

   ```bash
   python3 scripts/survey.py <skill> --samples 5
   ```

3. No start found → stop and tell the user: there is nothing to observe yet.

## 2. Write the profile

Write `<skills>/observe-<skill>/config.json` from the survey, field by field (`references/config.md`):

| Survey section                   | Profile field                                                                |
| -------------------------------- | ---------------------------------------------------------------------------- |
| Starts, former names             | `skill`, `aliases`                                                           |
| Subagents told to read the skill | `subagent_run` only when such a subagent runs the skill end to end           |
| Agent descriptions               | `phases`: one phase per recurring role, keywords taken from the descriptions |
| Headings, bold labels, PR links  | `report`, `outcome`, `outcome_keywords`, `delivered`, `final_outcomes`       |
| Slow commands of the repository  | `commands`: what the generic rules do not classify (`references/method.md`)  |

Keep only fields with evidence: a guessed pattern silently drops or splits runs.

## 3. Map every step of the skill

The span view and the analysis follow the skill's own steps: list them all in `steps`, in the skill's order, even the ones without a trace.

1. Take the steps from the survey's _Steps of the skill_; name each step as the skill does.
2. Give each step markers that only it produces (`references/config.md` → `steps`): a reference it alone reads, a script or command it alone runs, its subagents, its step number in the agent text. A candidate marked ⚠ (named by another step) is a marker only when its first use is unambiguous; otherwise drop it.
3. A step with no marker stays in the list: the dashboard shows it _never detected_, and the skill states it as a limit.

## 4. Build and check

```bash
python3 scripts/observe.py --config <skills>/observe-<skill>/config.json build
```

Check, and fix the profile until each holds:

- The run count matches the survey's starts (less the re-invocations it merges).
- `build` prints no run whose causes do not sum to its duration.
- `other agent` holds a minority of the subagent time; `NO REPORT` runs really ended without a result (read two of their final messages).
- The slowest _shell read/edit_ and _other tools_ calls (`observe.py --config <config> calls <transcript>`) hide no repository tool that deserves a `commands` rule.
- The 3 longest runs read right in "Run spans" (`serve`), steps lane included. Explain each long wait (premature return, stall, human wait) from the transcript around it (`calls`, `steps`), never from what the run's own report says about itself.
- Each step switch is plausible: `observe.py --config <config> steps <transcript.jsonl>` on 3 runs prints every switch with the marker and the signal behind it; a step entered too early, or re-entered after the end, means a shared marker to drop.
- The _Steps_ table lists each step as detected, or as _never detected_ with the reason written in the skill's limits.

## 5. Write the skill and hand over

1. Write `<skills>/observe-<skill>/SKILL.md` from `references/skill-template.md`: what a run is for this skill, its steps, and the limits found in step 4. Add the frontmatter fields and conventions your repository requires.
2. Validate and commit it the way your repository does for skills.
3. Report to the skill owner: runs found, median and p90, the top steps and causes, the open questions of the profile; ask for their feedback before trusting the verdicts.

## Change the engine

Every observe skill runs on `scripts/observe.py`: a change goes with a test in `scripts/test_observe.py` (fictional skills only), and a rebuild of the existing observe skills to check that their runs did not move.
