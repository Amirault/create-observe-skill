# Observe profile (`config.json`)

The profile tells the engine (`scripts/observe.py`) what one run of the observed skill looks like. Every field but `name` and `skill` is optional; leave out what the survey gave no evidence for. Patterns are Python regular expressions.

| Field              | Meaning                                                                                                                                                                                                                                                                                                       | Default                              |
| ------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------ |
| `name`             | the observe skill, `observe-<skill>`; also the state folder `${XDG_STATE_HOME:-~/.local/state}/<name>/`                                                                                                                                                                                                       | required                             |
| `skill`            | the observed skill; a run starts at `/<skill>` typed by a person or a `Skill` tool call for it                                                                                                                                                                                                                | required                             |
| `aliases`          | former names of the skill (`survey.py` reads them from git history)                                                                                                                                                                                                                                           | `[]`                                 |
| `title`            | dashboard title                                                                                                                                                                                                                                                                                               | `<skill> runs`                       |
| `subagent_run`     | pattern on a subagent's first prompt that makes the subagent itself a run (an evaluation harness replaying the skill). Never for the subagents the skill launches (review, triage)                                                                                                                            | none                                 |
| `subagent_label`   | label of those runs on the dashboard                                                                                                                                                                                                                                                                          | `Subagent run`                       |
| `subagent_spec`    | pattern capturing the case from that first prompt; a trailing `-<14 digits>` timestamp is dropped to group the replays of one case                                                                                                                                                                            | none                                 |
| `spec`             | pattern capturing what was asked from the invocation arguments: the comparable population                                                                                                                                                                                                                     | none                                 |
| `spec_default`     | spec of an invocation whose arguments do not match `spec`                                                                                                                                                                                                                                                     | none                                 |
| `spec_label`       | display format of the spec, `{}` standing for the value (`"{} file(s)"`)                                                                                                                                                                                                                                      | `{}`                                 |
| `report`           | pattern of the final report heading; the run ends at its first match, else at its last message before the next human prompt                                                                                                                                                                                   | none                                 |
| `report_header`    | pattern on the report with groups (spec, increment, increments total)                                                                                                                                                                                                                                         | none                                 |
| `outcome`          | pattern whose group 1 is the outcome line of the final message                                                                                                                                                                                                                                                | `**Outcome:**` or `**Outcome**:`     |
| `outcome_keywords` | `[keyword, label]` pairs searched in the outcome line; the keyword written first wins                                                                                                                                                                                                                         | PR OPENED, ABORTED, BLOCKED, SUCCESS |
| `final_outcomes`   | `[pattern, label]` pairs tried on the final message when it has no outcome line and no pull request link (`ASKED HUMAN`)                                                                                                                                                                                      | `[]`                                 |
| `delivered`        | outcome labels that count as delivered; a final message linking a pull request is PR OPENED when no outcome line exists                                                                                                                                                                                       | `["PR OPENED", "SUCCESS"]`           |
| `phases`           | `{phase: [keywords]}` matched, lowercased, in the description (else the first prompt) of each subagent the run launches; no match → `other agent`                                                                                                                                                             | `{}`                                 |
| `commands`         | `[{"cause", "pattern", "group"}]`: shell commands of the repository, tried before the generic rules on the command without heredocs, quotes and comments; `group` is a dashboard group (`Build & test`, `Pre-commit suite`, `Git & GitHub`, `Other tools`, …)                                                 | `[]`                                 |
| `steps`            | `[{"name", "markers"}]`: the skill's steps in its order; a marker is a pattern on `Bash: <command>`, `Read: <path>` (also `Edit:`, `Write:`), `Agent: <description and prompt start>`, `Skill: <name>` or `Text: <agent message>`; the run starts in the first step and enters a step at the last marker seen | `[]`                                 |

Changing the profile extracts every session again on the next scan: iterate freely.

## Example

A skill `tidy-code` that ends with a pull request, launches triage and review subagents, runs a linter the generic rules do not know, and has three steps:

```json
{
  "name": "observe-tidy-code",
  "skill": "tidy-code",
  "spec": "(\\d+)\\s*files?",
  "spec_default": "1",
  "spec_label": "{} file(s)",
  "final_outcomes": [["(?i)let me know|do you want", "ASKED HUMAN"]],
  "phases": { "triage": ["triage"], "review": ["review"] },
  "commands": [
    { "cause": "lint", "pattern": "\\blintkit\\b", "group": "Build & test" }
  ],
  "steps": [
    {
      "name": "Step 1 — Pick the files",
      "markers": ["^Agent: .*triage", "picking-rules\\.md"]
    },
    { "name": "Step 2 — Clean", "markers": ["^Bash: .*\\blintkit --fix\\b"] },
    {
      "name": "Step 3 — Review and PR",
      "markers": ["^Agent: .*review", "^Bash: .*\\bgh pr create\\b"]
    }
  ]
}
```
