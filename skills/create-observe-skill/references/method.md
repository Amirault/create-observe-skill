# Method

How `scripts/observe.py` turns transcripts into runs, steps, causes and verdicts. What is specific to one skill (names, report, outcomes, phases, steps, repository commands) comes from its profile, `observe-<skill>/config.json` (`references/config.md`).

## Runs

| Kind         | Starts                                                                                                                                        | Ends                                                                              |
| ------------ | --------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------- |
| Interactive  | `/<skill>` or a `Skill` tool call for it (a re-invocation with no human prompt in between stays the same run), or a `continue` after a report | its report (`report`); without one, its last message before the next human prompt |
| Subagent run | a subagent whose first prompt matches `subagent_run` (an evaluation harness replaying the skill)                                              | its last event                                                                    |

Each Agent call resolves to its subagent transcript (`subagents/agent-<id>.meta.json` → `toolUseId`, else the `agentId` in the call result). The call description names the phase, by the keywords of the profile's `phases`.

## Attribution

Each gap between two transcript events gets exactly one cause:

| State during the gap                                                                                        | Cause                                                                                                     |
| ----------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------- |
| a tool call runs (the one ending last when several run)                                                     | its category, below                                                                                       |
| no tool runs, the model speaks next                                                                         | LLM                                                                                                       |
| no tool runs, a person speaks next                                                                          | human wait                                                                                                |
| the orchestrator waits on a subagent (foreground call, background notification, coordinator nudge, polling) | what that subagent did, recursively; uncovered time → stalls & handoffs (agent overhead, background wait) |
| the platform re-asks after a cut-off or errored stream                                                      | stream stall                                                                                              |
| the orchestrator ended its turn early, until the coordinator nudged it                                      | premature return                                                                                          |

Shell commands are classified on their skeleton (heredoc bodies, quoted strings and comments removed): the profile's `commands` first, then these generic rules; the first match wins:

| Command                                    | Cause                   |
| ------------------------------------------ | ----------------------- |
| `git commit`                               | git commit (runs hooks) |
| `npm test`, `pytest`, `go test`, `make`, … | build & test            |
| `sleep`, `until`                           | sleep / polling         |
| `gh`                                       | GitHub CLI              |
| `git push`, `git fetch`, `git switch`, …   | git sync                |
| any other `git`                            | git local               |
| anything else                              | shell read/edit         |

## Steps

When the profile lists `steps`, each run starts in the first one and enters a step at each marker matching one of its signals: a tool call (`Bash:` command skeleton, `Read:`/`Edit:`/`Write:` path, `Agent:` description and prompt start, `Skill:` name) or an agent message (`Text:`). The first step listed whose marker matches wins. Every second of the flattened timeline then also belongs to the step in progress (`by_step`, split by cause group in `by_step_group`); `steps_seen` lists the steps a marker revealed.

## Outcome

- Outcome: the `outcome` line of the report (default `**Outcome**`, both bold styles), mapped by `outcome_keywords`; the keyword written first wins (`ABORTED, no PR opened` → ABORTED).
- No outcome line: a final message linking a pull request → PR OPENED, marked inferred; else the first matching `final_outcomes` label; else NO REPORT.
- Delivered: the profile's `delivered` labels.
- Pre-commit execution: a `git commit`, or a profile command of the _Pre-commit suite_ group, of 10 s or more; a shorter call failed before the hooks ran.

## Verdict

- The current window is compared with the previous window of the same size: durations over delivered runs, delivery rate over all runs.
- Permutation test, 2,000 seeded shuffles, two-sided on the window statistic (median, p90 or mean): Better or Worse at p < 0.05, Leaning under 0.2, Too few runs under 3 values per window.
- Headline: median delivered run and delivery rate; Mixed when one improves and the other degrades.

## Storage and retention

Every candidate session (a transcript mentioning the skill) is stored in the state folder as `sessions/<session-id>.jsonl`:

```text
{"session": {"id", "project", "path", "fingerprint", "extractor", "extracted_at", "runs"}}
{"run": {...}}   one line per run, none for a session that only mentions the skill
```

- The fingerprint hashes the name, modification time and size of the session transcript and of every file of its `subagents/` folder. A scan extracts a session again only when its fingerprint, `EXTRACTOR_VERSION` or the profile changed; the other sessions come from the store.
- Claude Code prunes transcripts. A pruned session keeps its file; its runs are flagged archived, with the values of their `extractor` version, and fields that version did not record stay empty.
- A session extracted again keeps its stored runs whose transcript is gone.
- Files are replaced atomically; an unreadable file is skipped and its session extracted again.
