#!/usr/bin/env python3
"""Observe the runs of one skill from local Claude Code transcripts.

Scans ~/.claude/projects, rebuilds every run of the skill named by a profile (config.json)
as a span tree (orchestrator and its subagents), attributes each second of wall-clock time
to exactly one cause and writes a self-contained dashboard.

    python3 observe.py --config <config.json> build          # <state>/sessions/*.jsonl + <state>/dashboard.html
    python3 observe.py --config <config.json> serve [--port N]   # live dashboard, Refresh rescans transcripts
    python3 observe.py --config <config.json> calls <transcript> [--cause C]  # every tool call of a run tree
    python3 observe.py --config <config.json> steps <transcript>  # each change of step and the marker behind it
"""

import argparse
import hashlib
import http.server
import json
import os
import re
import statistics
import sys
import threading
import time
import webbrowser
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_ROOT = os.path.expanduser("~/.claude/projects")
STATE_ROOT = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
SESSIONS_DIR = "sessions"
EXTRACTOR_VERSION = 9
SUBAGENT_KIND = "subagent"
TOP_TOOL_CALLS_PER_RUN = 40
MIN_TOOL_CALL_SECONDS = 5
MIN_SUITE_SECONDS = 10
MAX_UNATTRIBUTED_SECONDS = 1
PORT_ATTEMPTS = 20

CONTINUE_RE = re.compile(r"^\s*(continu\w*|go on|next increment|retry|try again|r[ée]essa\w*|reprends?)\b", re.I)
REMINDER_RE = re.compile(r"(?s)<system-reminder>.*?</system-reminder>")
QUOTED_SLEEP_RE = re.compile(r"\btime\.sleep\(")
DEFAULT_OUTCOME = r"\*\*Outcome(?::\*\*|\*\*:?)\s*([^\n]+)"
DURATION_RE = re.compile(r"\*\*Duration(?::\*\*|\*\*:?)\s*([^\n]+)")
PR_REFERENCE_RE = re.compile(r"/pull/\d+|\bPR\s*#\d+", re.I)
PR_CREATION_RE = re.compile(r"^Bash: .*(?:\bpr\s+create\b|\bhub\s+pull-request\b)|^Skill: \S*create-pr|"
                            r"^\S*(?:create_pull_request|create_pr)\b", re.I)
TIMESTAMP_SUFFIX_RE = re.compile(r"-\d{14}$")
AGENT_ID_RE = re.compile(r"agentId:\s*([0-9a-f]{8,})")
NOTIFICATION_TOOL_RE = re.compile(r"<tool-use-id>(toolu_\w+)</tool-use-id>")

GIT = r"\bgit\s+(?:-[cC]\s+\S+\s+)*"
GIT_COMMIT = "git commit (runs hooks)"
BASH_CATEGORIES = [
    (GIT_COMMIT, re.compile(GIT + r"commit\b")),
    ("build & test", re.compile(r"\b(?:dotnet\s+(?:build|test|restore|run)|(?:npm|pnpm|yarn)\s+(?:test|run|ci|build)|pytest|"
                                r"go\s+(?:test|build)|cargo\s+(?:test|build)|mvn|gradlew?|make|vitest|jest)\b")),
    ("sleep / polling", re.compile(r"\bsleep\b|time\.sleep|(^|\n)\s*until\b|\bwhile\b[^\n]*\bsleep\b")),
    ("GitHub CLI", re.compile(r"(^|[\s;&|(])gh\s")),
    ("git sync", re.compile(GIT + r"(push|fetch|pull|switch|checkout|worktree|rebase|merge|clone)\b")),
    ("git local", re.compile(r"\bgit\s")),
]
HEREDOC_RE = re.compile(r"<<-?\s*(['\"]?)(\w+)\1([^\n]*)\n.*?\n[ \t]*\2[ \t]*(?=\n|$)", re.S)
QUOTED_RE = re.compile(r"'[^']*'|\"(?:\\.|[^\"\\])*\"")
COMMENT_RE = re.compile(r"(^|[ \t])#[^\n]*", re.M)
FILE_TOOLS = {"Read", "Edit", "Write", "Grep", "Glob", "MultiEdit", "NotebookEdit"}
AGENT_TOOLS = {"Agent", "Task"}
WAIT_TOOLS = {"TaskOutput", "AgentOutputTool", "BashOutput", "Monitor"}
HUMAN_TOOLS = {"AskUserQuestion", "ExitPlanMode"}

GROUPS = [
    ("LLM", ["LLM"]),
    ("Build & test", ["build & test"]),
    ("Pre-commit suite", [GIT_COMMIT]),
    ("Git & GitHub", ["git sync", "git local", "GitHub CLI"]),
    ("Other tools", ["shell read/edit", "file tools", "other tools"]),
    ("Sleep / polling", ["sleep / polling"]),
    ("Human wait", ["human wait"]),
    ("Stalls & handoffs", ["stream stall", "premature return", "background wait", "agent overhead"]),
]
DEFAULT_OUTCOME_KEYWORDS = [["PR OPENED", "PR OPENED"], ["PULL REQUEST OPENED", "PR OPENED"], ["ABORTED", "ABORTED"],
                            ["BLOCKED", "BLOCKED"], ["SUCCESS", "SUCCESS"]]
DEFAULT_DELIVERED = ["PR OPENED", "SUCCESS"]


class Profile:
    """What makes one skill observable: its names, how its runs start and end, its outcomes and phases."""

    def __init__(self, config):
        self.config = config
        self.name = config["name"]
        self.skill = config["skill"]
        self.names = [config["skill"]] + config.get("aliases", [])
        alternatives = "|".join(re.escape(n) for n in sorted(self.names, key=len, reverse=True))
        self.title = config.get("title", self.skill + " runs")
        self.invocation = re.compile(r"^\s*(?:<command-message>[^<]*</command-message>\s*)?(?:<command-name>)?/("
                                     + alternatives + r")\b")
        self.subagent_run = optional_pattern(config.get("subagent_run"))
        self.subagent_label = config.get("subagent_label", "Subagent run")
        self.subagent_spec = optional_pattern(config.get("subagent_spec"))
        self.report = optional_pattern(config.get("report"))
        self.report_header = optional_pattern(config.get("report_header"))
        self.outcome = re.compile(config.get("outcome", DEFAULT_OUTCOME))
        self.outcome_keywords = [tuple(pair) for pair in config.get("outcome_keywords", DEFAULT_OUTCOME_KEYWORDS)]
        self.delivered = set(config.get("delivered", DEFAULT_DELIVERED))
        self.spec = optional_pattern(config.get("spec"))
        self.spec_default = config.get("spec_default")
        self.final_outcomes = [(re.compile(pattern), label) for pattern, label in config.get("final_outcomes", [])]
        self.phase_keywords = config.get("phases", {})
        self.phases = ["orchestrator"] + list(self.phase_keywords) + ["other agent"]
        self.commands = [(c["cause"], re.compile(c["pattern"]), c["group"]) for c in config.get("commands", [])]
        self.group_leaves = {group: list(leaves) for group, leaves in GROUPS}
        for cause, _, group in self.commands:
            self.group_leaves[group] += [cause] if cause not in self.group_leaves[group] else []
        self.group_by_cause = {leaf: group for group, leaves in self.group_leaves.items() for leaf in leaves}
        self.steps = [(step["name"], [re.compile(marker, re.I | re.M) for marker in step.get("markers", [])])
                      for step in config.get("steps", [])]
        self.digest = hashlib.sha1(json.dumps(config, sort_keys=True).encode()).hexdigest()

    def mentioned_in(self, text):
        return any(name in text for name in self.names)

    def public(self):
        return {"name": self.name, "skill": self.skill, "title": self.title, "subagent_label": self.subagent_label,
                "steps": [name for name, _ in self.steps], "spec_label": self.config.get("spec_label", "{}")}


def group_of(category):
    return PROFILE.group_by_cause.get(category, "Other tools")


def optional_pattern(pattern):
    return re.compile(pattern) if pattern else None


def load_profile(path):
    return Profile(read_json(path))


PROFILE = None


def use_profile(profile):
    global PROFILE
    PROFILE = profile


def read_text(path):
    with open(path) as handle:
        return handle.read()


def read_json(path):
    with open(path) as handle:
        return json.load(handle)


def write_json(path, data):
    with open(path, "w") as handle:
        json.dump(data, handle)


def session_dir_of(path):
    """A subagent transcript lives in <session>/subagents/; a session transcript is <session>.jsonl."""
    if os.path.basename(os.path.dirname(path)) == "subagents":
        return os.path.dirname(os.path.dirname(path))
    return path[:-len(".jsonl")]


def parse_ts(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def iso(seconds):
    return datetime.fromtimestamp(seconds, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def text_of(content):
    if isinstance(content, str):
        return content
    return " ".join(b.get("text", "") for b in content or [] if isinstance(b, dict) and b.get("type") == "text")


def strip_shell_noise(command):
    command = re.sub(r"^\s*(cd\s+[^&;\n]+(&&|;|\n)\s*)+", "", command)
    return re.sub(r"\s+", " ", command).strip()


def command_skeleton(command):
    """The command without heredoc bodies, quoted strings and comments: text in them never classifies it."""
    command = HEREDOC_RE.sub(lambda m: "<<heredoc" + m.group(3), command)
    return COMMENT_RE.sub(r"\1", QUOTED_RE.sub("''", command))


def classify_tool(name, tool_input):
    """Returns (leaf category, expandable) for a pending tool call."""
    if name in AGENT_TOOLS:
        return "agent overhead", True
    if name in WAIT_TOOLS:
        return "sleep / polling" if name == "Monitor" else "background wait", True
    if name in HUMAN_TOOLS:
        return "human wait", False
    if name in FILE_TOOLS:
        return "file tools", False
    if name != "Bash":
        return "other tools", False
    raw = tool_input.get("command", "")
    command = command_skeleton(raw)
    for category, pattern in [(cause, pattern) for cause, pattern, _ in (PROFILE.commands if PROFILE else [])] + BASH_CATEGORIES:
        if pattern.search(command) or (category == "sleep / polling" and QUOTED_SLEEP_RE.search(raw)):
            return category, category == "sleep / polling"
    return "shell read/edit", False


def classify_user_text(text, is_meta, origin):
    stripped = text.strip()
    if origin == "coordinator" or "The coordinator sent a message while you were working" in stripped:
        return "coordinator"
    if origin == "task-notification" or stripped.startswith("<task-notification>"):
        return "notification"
    if "was cut off mid-stream" in stripped:
        return "stall"
    if is_meta or not stripped or stripped.startswith("Base directory for this skill"):
        return "neutral"
    if re.fullmatch(r"(?s)(<system-reminder>.*?</system-reminder>\s*)+", stripped):
        return "neutral"
    if stripped.startswith(("<local-command-stdout>", "<local-command-caveat>")):
        return "neutral"
    return "human"


def phase_of(description, prompt):
    text = (description or prompt[:300] or "").lower()
    for phase, keys in PROFILE.phase_keywords.items():
        if any(k in text for k in keys):
            return phase
    return "other agent"


class Transcript:
    def __init__(self, path):
        self.path = path
        self.agent_id = None
        base = os.path.basename(path)
        if base.startswith("agent-"):
            self.agent_id = base[len("agent-"):-len(".jsonl")]
        self.events = []
        self.tools = {}
        self.usage = {}
        self.first_prompt = ""
        self._load()
        self.start = self.events[0]["t"] if self.events else 0
        self.end = self.events[-1]["t"] if self.events else 0

    def _load(self):
        with open(self.path, errors="ignore") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                kind = record.get("type")
                if kind not in ("user", "assistant") or "timestamp" not in record:
                    continue
                t = parse_ts(record["timestamp"])
                message = record.get("message") or {}
                if kind == "assistant":
                    self._load_assistant(t, message, record.get("isApiErrorMessage"))
                else:
                    self._load_user(t, record, message)
        self.events.sort(key=lambda e: e["t"])

    def _load_assistant(self, t, message, api_error):
        usage = message.get("usage")
        if usage and message.get("id"):
            self.usage[message["id"]] = (message.get("model", "?"), usage, t)
        texts = []
        for block in message.get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                tool_input = block.get("input") or {}
                self.tools[block["id"]] = {
                    "t0": t, "t1": None, "name": block.get("name"), "input": tool_input,
                    "error": False, "agent_id": None,
                    "background": bool(tool_input.get("run_in_background")),
                }
            elif block.get("type") == "text":
                texts.append(block.get("text", ""))
        self.events.append({"t": t, "kind": "stall" if api_error else "assistant", "text": "\n".join(texts),
                            "message_id": message.get("id")})

    def _load_user(self, t, record, message):
        content = message.get("content")
        if isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                tool = self.tools.get(block.get("tool_use_id"))
                if not tool:
                    continue
                tool["t1"] = t
                tool["error"] = bool(block.get("is_error"))
                found = AGENT_ID_RE.search(json.dumps(block.get("content"))) or AGENT_ID_RE.search(
                    json.dumps(record.get("toolUseResult") or ""))
                if found:
                    tool["agent_id"] = found.group(1)
            self.events.append({"t": t, "kind": "tool_result"})
            return
        text = text_of(content)
        if not self.first_prompt and text.strip():
            self.first_prompt = text
        kind = classify_user_text(text, record.get("isMeta"), (record.get("origin") or {}).get("kind"))
        event = {"t": t, "kind": kind, "text": text}
        if kind == "notification":
            found = NOTIFICATION_TOOL_RE.search(text)
            event["tool_use_id"] = found.group(1) if found else None
        self.events.append(event)

    def model_turns(self, start, end):
        return len({e.get("message_id") or e["t"] for e in self.events
                    if e["kind"] == "assistant" and start <= e["t"] <= end})

    def tokens(self, start, end):
        totals = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
        models = {}
        for model, usage, t in self.usage.values():
            if not start <= t <= end:
                continue
            totals["input"] += usage.get("input_tokens", 0) or 0
            totals["output"] += usage.get("output_tokens", 0) or 0
            totals["cache_read"] += usage.get("cache_read_input_tokens", 0) or 0
            totals["cache_write"] += usage.get("cache_creation_input_tokens", 0) or 0
            models[model] = models.get(model, 0) + 1
        return totals, models


class Session:
    """Indexes a session's subagent transcripts so Agent tool calls resolve to their child."""

    def __init__(self, session_dir):
        self.dir = session_dir
        self.by_tool_use = {}
        self.by_agent_id = {}
        self.meta = {}
        self._cache = {}
        sub = os.path.join(session_dir, "subagents")
        if not os.path.isdir(sub):
            return
        for name in os.listdir(sub):
            if not name.endswith(".jsonl"):
                continue
            agent_id = name[len("agent-"):-len(".jsonl")]
            path = os.path.join(sub, name)
            self.by_agent_id[agent_id] = path
            meta_path = path[:-len(".jsonl")] + ".meta.json"
            if os.path.exists(meta_path):
                try:
                    meta = read_json(meta_path)
                except ValueError:
                    continue
                self.meta[agent_id] = meta
                if meta.get("toolUseId"):
                    self.by_tool_use[meta["toolUseId"]] = agent_id

    def transcript(self, agent_id):
        if agent_id not in self._cache:
            path = self.by_agent_id.get(agent_id)
            self._cache[agent_id] = Transcript(path) if path else None
        return self._cache[agent_id]

    def child_of(self, tool_use_id, tool):
        agent_id = self.by_tool_use.get(tool_use_id) or tool.get("agent_id")
        return agent_id if agent_id in self.by_agent_id else None


class SpanBuilder:
    """Turns a transcript window into flat, non-overlapping (start, end, actor, category) intervals."""

    def __init__(self, session):
        self.session = session
        self.calls = []
        self.children = {}

    def children_of(self, transcript, start, end):
        found = {}
        for tool_use_id, tool in transcript.tools.items():
            if tool["name"] in AGENT_TOOLS and start <= tool["t0"] <= end:
                agent_id = self.session.child_of(tool_use_id, tool)
                if agent_id:
                    found[tool_use_id] = agent_id
        return found

    def flatten(self, transcript, start, end, actor):
        children = self.children_of(transcript, start, end)
        for tool_use_id, agent_id in children.items():
            self._register_child(agent_id, transcript.tools[tool_use_id])
        intervals = []
        for a, b, category, detail, failed, expand in self.own_intervals(transcript, start, end, actor, children):
            if not expand:
                intervals.append((a, b, actor, category, detail, failed))
                continue
            intervals.extend(self._expand(a, b, actor, category, detail, expand))
        return intervals

    def _register_child(self, agent_id, tool):
        if agent_id in self.children:
            return
        child = self.session.transcript(agent_id)
        if not child or not child.events:
            return
        meta = self.session.meta.get(agent_id, {})
        description = meta.get("description") or tool["input"].get("description", "")
        self.children[agent_id] = {
            "transcript": child,
            "phase": phase_of(description, child.first_prompt),
            "description": description,
            "background": meta.get("requestShape") == "background" or bool(tool["input"].get("run_in_background")),
        }

    def _expand(self, a, b, actor, fallback, detail, candidates):
        windows = [(self.children[c]["transcript"].start, self.children[c]["transcript"].end, c)
                   for c in candidates if c in self.children]
        cuts = sorted({a, b} | {t for s, e, _ in windows for t in (s, e) if a < t < b})
        result = []
        for lo, hi in zip(cuts, cuts[1:]):
            active = [(e, c) for s, e, c in windows if s <= lo and e >= hi]
            if not active:
                result.append((lo, hi, actor, fallback, detail, False))
                continue
            agent_id = max(active)[1]
            for item in self._child_intervals(agent_id):
                ca, cb = max(item[0], lo), min(item[1], hi)
                if cb > ca:
                    result.append((ca, cb) + item[2:])
        return result

    def complete_children(self):
        """Flattens the subagents no wait interval reached, so their stats are complete."""
        for agent_id in list(self.children):
            self._child_intervals(agent_id)

    def _child_intervals(self, agent_id):
        child = self.children[agent_id]
        if "intervals" not in child:
            transcript = child["transcript"]
            child["intervals"] = []
            child["intervals"] = self.flatten(transcript, transcript.start, transcript.end, child["phase"])
        return child["intervals"]

    def own_intervals(self, transcript, start, end, actor, children):
        events = [e for e in transcript.events if start <= e["t"] <= end and e["kind"] != "neutral"]
        tools = [(tid, tool) for tid, tool in transcript.tools.items() if start <= tool["t0"] <= end]
        self.calls.extend(describe_call(tool, actor, end) for _, tool in tools if tool["name"] not in AGENT_TOOLS)
        any_child = list(children.values())
        for current, following in zip(events, events[1:]):
            a, b = current["t"], following["t"]
            if b <= a:
                continue
            pending = [(tool["t1"] or end, tid, tool) for tid, tool in tools
                       if tool["t0"] <= a and (tool["t1"] is None or tool["t1"] > a) and not
                       (tool["background"] and tool["name"] == "Bash")]
            if pending:
                _, tid, tool = max(pending, key=lambda p: p[0])
                category, expandable = classify_tool(tool["name"], tool["input"])
                detail = tool["name"]
                expand = None
                if expandable:
                    expand = [children[tid]] if tid in children else any_child
                yield a, b, category, detail, tool["error"], expand
                continue
            kind = following["kind"]
            if kind == "assistant" or kind == "tool_result":
                yield a, b, "LLM", "", False, None
            elif kind == "stall":
                yield a, b, "stream stall", "", False, None
            elif kind == "human":
                yield a, b, "human wait", "", False, None
            elif kind == "coordinator":
                yield a, b, "premature return", "", False, any_child
            elif kind == "notification":
                target = children.get(following.get("tool_use_id"))
                yield a, b, "background wait", "", False, [target] if target else any_child
            else:
                yield a, b, "LLM", "", False, None


def describe_call(tool, actor, end):
    tool_input = tool["input"]
    label = strip_shell_noise(tool_input.get("command", "")) if tool["name"] == "Bash" else \
        tool["name"] + " " + str(tool_input.get("file_path") or tool_input.get("pattern") or "")
    return {"actor": actor, "tool": tool["name"], "category": classify_tool(tool["name"], tool_input)[0],
            "seconds": round((tool["t1"] or end) - tool["t0"], 1), "failed": tool["error"],
            "command": label[:220], "start": iso(tool["t0"])}


def list_calls(transcript_path, cause=None):
    """Every tool call of a transcript and of the subagents it launched, oldest first."""
    transcript = Transcript(transcript_path)
    builder = SpanBuilder(Session(session_dir_of(transcript_path)))
    builder.flatten(transcript, transcript.start, transcript.end, "orchestrator")
    builder.complete_children()
    return sorted((c for c in builder.calls if cause in (None, c["category"])), key=lambda c: c["start"])


def merge_intervals(intervals):
    merged = []
    for a, b, actor, category, detail, failed in sorted(intervals):
        if merged and merged[-1][2] == actor and merged[-1][3] == category and abs(merged[-1][1] - a) < 1e-6:
            merged[-1][1] = b
            merged[-1][5] = merged[-1][5] or failed
            continue
        merged.append([a, b, actor, category, detail, failed])
    return merged


def hook_stats(calls, categories):
    """Attempts include calls that failed before the hooks ran; executions last long enough to have run them."""
    attempts = [c for c in calls if c["category"] in categories]
    executions = [c for c in attempts if c["seconds"] >= MIN_SUITE_SECONDS]
    by_actor = {}
    for call in executions:
        by_actor[call["actor"]] = by_actor.get(call["actor"], 0) + 1
    return {"count": len(attempts), "failed": sum(1 for c in attempts if c["failed"]),
            "executions": len(executions), "durations": sorted(c["seconds"] for c in executions),
            "by_actor": by_actor}


def signal_of(tool):
    """What a tool call says about the step the run is in, prefixed by the tool name for markers to anchor on."""
    tool_input = tool["input"]
    if tool["name"] == "Bash":
        text = re.sub(r"\s+", " ", command_skeleton(tool_input.get("command", "")))
    elif tool["name"] in AGENT_TOOLS:
        text = tool_input.get("description", "") + " " + tool_input.get("prompt", "")[:300]
    elif tool["name"] == "Skill":
        text = str(tool_input.get("skill", ""))
    else:
        text = str(tool_input.get("file_path") or tool_input.get("path") or tool_input.get("pattern") or "")
    return tool["name"] + ": " + text


def step_matches(transcript, start, end):
    """Every signal of the window that matches a step marker, oldest first: (time, step, marker, signal)."""
    signals = [(tool["t0"], signal_of(tool)) for tool in transcript.tools.values() if start <= tool["t0"] <= end]
    signals += [(e["t"], "Text: " + e["text"]) for e in transcript.events
                if e["kind"] == "assistant" and e["text"] and start <= e["t"] <= end]
    matches = []
    for t, signal in sorted(signals, key=lambda s: s[0]):
        found = next(((name, m) for name, markers in PROFILE.steps for m in markers if m.search(signal)), None)
        if found:
            matches.append((t, found[0], found[1].pattern, signal))
    return matches


def step_spans(transcript, start, end):
    """The steps of the profile the run went through, entered at the last marker seen: (start, end, step), seen steps."""
    current, since, seen, spans = PROFILE.steps[0][0], start, [], []
    for t, step, _, _ in step_matches(transcript, start, end):
        seen += [step] if step not in seen else []
        if step != current:
            spans.append((since, t, current))
            current, since = step, t
    spans.append((since, end, current))
    return [span for span in spans if span[1] > span[0]], seen


def step_switches(transcript_path):
    """Each change of step in a transcript with the marker and the signal behind it, to check the markers by hand."""
    transcript = Transcript(transcript_path)
    switches, current = [], PROFILE.steps[0][0]
    for t, step, marker, signal in step_matches(transcript, transcript.start, transcript.end):
        if step != current:
            switches.append({"offset": round(t - transcript.start, 1), "at": iso(t), "step": step, "marker": marker,
                             "signal": re.sub(r"\s+", " ", signal)[:160]})
            current = step
    return switches


def time_by_step(intervals, spans):
    by_step, by_step_group = {}, {}
    for a, b, _, category, _, _ in intervals:
        for s0, s1, step in spans:
            overlap = min(b, s1) - max(a, s0)
            if overlap <= 0:
                continue
            group = group_of(category)
            by_step[step] = by_step.get(step, 0) + overlap
            by_step_group.setdefault(step, {})
            by_step_group[step][group] = by_step_group[step].get(group, 0) + overlap
    return by_step, by_step_group


def find_report(transcript, start, end):
    for event in transcript.events:
        if event["kind"] == "assistant" and start <= event["t"] <= end and PROFILE.report and \
                PROFILE.report.search(event.get("text", "")):
            return event
    return None


def final_message(transcript, start, end):
    texts = [e for e in transcript.events if e["kind"] == "assistant" and start <= e["t"] <= end and e["text"].strip()]
    return texts[-1]["text"] if texts else ""


def created_pull_request(transcripts_and_windows):
    """Whether the run itself created a pull request: a final message may quote one it did not open."""
    return any(PR_CREATION_RE.search(signal_of(tool))
               for transcript, start, end in transcripts_and_windows
               for tool in transcript.tools.values() if start <= tool["t0"] <= end)


def report_fields(report_text, final_text, created_pr):
    """Reads the final report; without one, infers the outcome from the run's final message."""
    fields = {}
    text = report_text or final_text
    header = PROFILE.report_header.search(report_text) if PROFILE.report_header else None
    if header:
        fields["report_spec"] = header.group(1)
        fields["increment"] = int(header.group(2))
        fields["increments_total"] = int(header.group(3)) if header.group(3) else None
    outcome = PROFILE.outcome.search(text)
    if outcome:
        fields["outcome_raw"] = re.sub(r"[*`]", "", outcome.group(1)).strip()[:200]
        fields["outcome"] = outcome_label(fields["outcome_raw"])
    elif created_pr and PR_REFERENCE_RE.search(final_text):
        fields["outcome_raw"] = re.sub(r"\s+", " ", final_text).strip()[:200]
        fields["outcome"] = "PR OPENED"
        fields["outcome_inferred"] = True
    else:
        fields["outcome_raw"] = re.sub(r"\s+", " ", final_text).strip()[:200] or None
        fields["outcome"] = next((label for pattern, label in PROFILE.final_outcomes if pattern.search(final_text)),
                                 "NO REPORT")
        fields["outcome_inferred"] = fields["outcome"] != "NO REPORT"
    duration = DURATION_RE.search(text)
    if duration:
        fields["reported_duration"] = re.sub(r"[*`]", "", duration.group(1)).strip()[:300]
    return fields


def outcome_label(raw):
    """The keyword written first wins: 'ABORTED, no PR opened' is ABORTED, not PR OPENED."""
    upper = raw.upper()
    found = [(upper.find(keyword), label) for keyword, label in PROFILE.outcome_keywords if keyword in upper]
    if found:
        return min(found)[1]
    return raw.split()[0][:24].upper() if raw.split() else "NO REPORT"


def without_relaunches(starts, transcript):
    """Drops a start with no human prompt since the previous one: the agent re-invoking the skill it was asked to run."""
    kept = []
    for start in starts:
        prompted = kept and any(e["kind"] == "human" and kept[-1][0] < e["t"] <= start[0] for e in transcript.events)
        if not kept or prompted:
            kept.append(start)
    return kept


def interactive_windows(transcript):
    """Returns (start, end, end_reason, trigger, skill, args, report) for each run of an interactive session."""
    starts = []
    for event in transcript.events:
        if event["kind"] != "human":
            continue
        found = PROFILE.invocation.search(event["text"])
        if found:
            starts.append([event["t"], "invocation", found.group(1), event["text"]])
    for tid, tool in transcript.tools.items():
        if tool["name"] == "Skill" and str(tool["input"].get("skill", "")).split(":")[-1] in PROFILE.names:
            starts.append([tool["t0"], "skill tool", tool["input"]["skill"], str(tool["input"].get("args", ""))])
    starts = without_relaunches(sorted(starts), transcript)
    windows = []
    index = 0
    while index < len(starts):
        start, trigger, skill, args = starts[index]
        limit = starts[index + 1][0] if index + 1 < len(starts) else float("inf")
        report = find_report(transcript, start, limit)
        if report:
            end, end_reason = report["t"], "report"
        else:
            humans = [e["t"] for e in transcript.events if e["kind"] == "human" and start < e["t"] < limit]
            cutoff = humans[0] if humans else min(limit, transcript.end + 1)
            before = [e["t"] for e in transcript.events if start <= e["t"] < cutoff]
            end = before[-1] if before else start
            end_reason = "last message before " + (
                "a human prompt" if humans else "the next invocation" if limit < float("inf") else "the transcript end")
        windows.append((start, end, end_reason, trigger, skill, args, report))
        continuation = next((e for e in transcript.events if e["kind"] == "human" and end < e["t"] < limit and
                             CONTINUE_RE.match(REMINDER_RE.sub("", e["text"]))), None)
        if continuation:
            starts.insert(index + 1, [continuation["t"], "continue", skill, args])
        index += 1
    return windows


def build_run(kind, transcript, session, start, end, end_reason, trigger, skill, args, report, project):
    builder = SpanBuilder(session)
    actor = "orchestrator"
    intervals = merge_intervals(builder.flatten(transcript, start, end, actor))
    builder.complete_children()
    tree = [(transcript, start, end)] + [(c["transcript"], c["transcript"].start, c["transcript"].end)
                                         for c in builder.children.values()]
    fields = report_fields(report["text"] if report else "", final_message(transcript, start, end),
                           created_pull_request(tree))
    spec_pattern = PROFILE.subagent_spec if kind == SUBAGENT_KIND else PROFILE.spec
    found = spec_pattern.search(transcript.first_prompt if kind == SUBAGENT_KIND else args or "") if spec_pattern else None
    spec = (found.group(1) if found else None) or fields.get("report_spec") or PROFILE.spec_default
    case = TIMESTAMP_SUFFIX_RE.sub("", spec) if spec and kind == SUBAGENT_KIND else None
    by_category, by_actor, by_actor_group = {}, {}, {}
    failed_seconds = 0.0
    segments = []
    for a, b, who, category, detail, failed in intervals:
        seconds = b - a
        by_category[category] = by_category.get(category, 0) + seconds
        by_actor[who] = by_actor.get(who, 0) + seconds
        group = group_of(category)
        by_actor_group.setdefault(who, {})
        by_actor_group[who][group] = by_actor_group[who].get(group, 0) + seconds
        if failed:
            failed_seconds += seconds
        segments.append([round(a - start, 1), round(seconds, 1), who, category, detail, failed])
    by_group = {}
    for category, seconds in by_category.items():
        group = group_of(category)
        by_group[group] = by_group.get(group, 0) + seconds
    duration = end - start
    tokens, models = transcript.tokens(start, end)
    subagents = []
    llm_turns = transcript.model_turns(start, end)
    tool_calls = sum(1 for t in transcript.tools.values() if start <= t["t0"] <= end)
    errors = sum(1 for t in transcript.tools.values() if start <= t["t0"] <= end and t["error"])
    for agent_id, child in sorted(builder.children.items(), key=lambda c: c[1]["transcript"].start):
        child_transcript = child["transcript"]
        child_tokens, child_models = child_transcript.tokens(child_transcript.start, child_transcript.end)
        for key in tokens:
            tokens[key] += child_tokens[key]
        for model, count in child_models.items():
            models[model] = models.get(model, 0) + count
        llm_turns += child_transcript.model_turns(child_transcript.start, child_transcript.end)
        tool_calls += len(child_transcript.tools)
        errors += sum(1 for t in child_transcript.tools.values() if t["error"])
        own = {}
        for a, b, who, category, _, _ in child.get("intervals", []):
            if who == child["phase"]:
                group = group_of(category)
                own[group] = own.get(group, 0) + (b - a)
        subagents.append({
            "agent_id": agent_id, "phase": child["phase"], "description": child["description"][:120],
            "background": child["background"], "start_offset": round(child_transcript.start - start, 1),
            "duration": round(child_transcript.end - child_transcript.start, 1),
            "by_group": {k: round(v, 1) for k, v in own.items()},
            "output_tokens": child_tokens["output"],
        })
    slow_calls = [c for c in builder.calls if c["seconds"] >= MIN_TOOL_CALL_SECONDS]
    tool_list = sorted(slow_calls, key=lambda c: -c["seconds"])[:TOP_TOOL_CALLS_PER_RUN]
    steps, seen, by_step, by_step_group = [], [], {}, {}
    if PROFILE.steps:
        spans, seen = step_spans(transcript, start, end)
        by_step, by_step_group = time_by_step(intervals, spans)
        steps = [[round(s0 - start, 1), round(s1 - s0, 1), step] for s0, s1, step in spans]
    run_id = (transcript.agent_id or os.path.basename(transcript.path)[:8]) + "@" + iso(start)
    return {
        "id": run_id, "kind": kind, "trigger": trigger, "skill": skill, "project": project,
        "session": os.path.basename(session.dir), "transcript": transcript.path,
        "extractor": EXTRACTOR_VERSION, "start": iso(start), "end": iso(end), "end_reason": end_reason,
        "duration": round(duration, 1), "active": round(duration - by_category.get("human wait", 0), 1),
        "spec": spec, "case": case, "increment": fields.get("increment"),
        "increments_total": fields.get("increments_total"),
        "outcome": fields["outcome"], "outcome_raw": fields.get("outcome_raw"),
        "outcome_inferred": bool(fields.get("outcome_inferred")),
        "delivered": fields["outcome"] in PROFILE.delivered,
        "reported_duration": fields.get("reported_duration"),
        "commits": hook_stats(builder.calls, {GIT_COMMIT}),
        "manual_hook_runs": hook_stats(builder.calls, set(PROFILE.group_leaves["Pre-commit suite"]) - {GIT_COMMIT}),
        "by_group": {k: round(v, 1) for k, v in by_group.items()},
        "by_category": {k: round(v, 1) for k, v in by_category.items()},
        "by_actor": {k: round(v, 1) for k, v in by_actor.items()},
        "by_actor_group": {a: {k: round(v, 1) for k, v in g.items()} for a, g in by_actor_group.items()},
        "failed_seconds": round(failed_seconds, 1),
        "review_iterations": sum(1 for s in subagents if s["phase"] == "review"),
        "implement_runs": sum(1 for s in subagents if s["phase"] == "implement"),
        "subagents": subagents, "segments": segments, "tool_calls": tool_list,
        "steps": steps, "steps_seen": seen, "by_step": {k: round(v, 1) for k, v in by_step.items()},
        "by_step_group": {k: {g: round(v, 1) for g, v in groups.items()} for k, groups in by_step_group.items()},
        "llm_turns": llm_turns, "tool_call_count": tool_calls, "tool_errors": errors,
        "tokens": tokens, "models": models,
    }


def candidate_files(root, cache):
    found = []
    for directory, _, names in os.walk(root):
        for name in names:
            if not name.endswith(".jsonl"):
                continue
            path = os.path.join(directory, name)
            stat = os.stat(path)
            key = [stat.st_mtime, stat.st_size]
            cached = cache.get(path)
            if cached and cached[:2] == key:
                hit = cached[2]
            else:
                with open(path, errors="ignore") as handle:
                    hit = PROFILE.mentioned_in(handle.read())
                cache[path] = key + [hit]
            if hit:
                found.append(path)
    return found


def session_fingerprint(session_dir):
    """Changes as soon as the session transcript or any file of its subagents/ folder changes."""
    subagents = os.path.join(session_dir, "subagents")
    names = sorted(os.listdir(subagents)) if os.path.isdir(subagents) else []
    entries = []
    for path in [session_dir + ".jsonl"] + [os.path.join(subagents, name) for name in names]:
        try:
            stat = os.stat(path)
        except OSError:
            continue
        entries.append("%s %d %d" % (os.path.basename(path), stat.st_mtime_ns, stat.st_size))
    return hashlib.sha1("\n".join(entries).encode()).hexdigest()


class SessionStore:
    """The extraction history: <state>/sessions/<session-id>.jsonl, a session line then one run line per run."""

    def __init__(self, state_dir):
        self.dir = os.path.join(state_dir, SESSIONS_DIR)
        self.sessions = {}
        if not os.path.isdir(self.dir):
            return
        for name in sorted(os.listdir(self.dir)):
            if name.endswith(".jsonl"):
                self._load(os.path.join(self.dir, name))

    def _load(self, path):
        header, runs = None, []
        try:
            with open(path) as handle:
                for line in handle:
                    record = json.loads(line)
                    header = record.get("session", header)
                    runs.extend([record["run"]] if "run" in record else [])
        except (OSError, ValueError) as error:
            print("unreadable %s, extracted again: %s" % (path, error), file=sys.stderr)
            return
        if header:
            self.sessions[header["id"]] = (header, runs)

    def is_current(self, session_id, fingerprint):
        header = self.sessions.get(session_id, ({}, []))[0]
        return (header.get("fingerprint"), header.get("extractor"), header.get("profile")) == (
            fingerprint, EXTRACTOR_VERSION, PROFILE.digest)

    def save(self, header, runs):
        """Replaces the runs of the session, keeping the stored ones whose transcript Claude Code has pruned since."""
        fresh = {run["id"] for run in runs}
        previous = self.sessions.get(header["id"], ({}, []))[1]
        runs = runs + [run for run in previous if run["id"] not in fresh and not os.path.exists(run["transcript"])]
        header = dict(header, runs=len(runs))
        os.makedirs(self.dir, exist_ok=True)
        path = os.path.join(self.dir, header["id"] + ".jsonl")
        with open(path + ".tmp", "w") as handle:
            for record in [{"session": header}] + [{"run": run} for run in runs]:
                handle.write(json.dumps(record) + "\n")
        os.replace(path + ".tmp", path)
        self.sessions[header["id"]] = (header, runs)

    def runs(self):
        runs = []
        for _, session_runs in self.sessions.values():
            for run in session_runs:
                if not os.path.exists(run["transcript"]):
                    run["archived"] = True
                    upgrade_archived(run)
                runs.append(run)
        return sorted(runs, key=lambda r: r["start"])


def upgrade_archived(run):
    """Fills the fields an older extractor did not write; unknown values stay None, never guessed."""
    if run.get("extractor") is None:
        run["extractor"] = 1
        run["llm_turns"] = None
    run.setdefault("end_reason", "unknown (archived)")
    run.setdefault("outcome_inferred", False)
    run.setdefault("delivered", run.get("outcome") in PROFILE.delivered)
    run.setdefault("commits", None)
    run.setdefault("manual_hook_runs", None)
    run.setdefault("increments_total", None)
    return run


def extract_runs(path, session, project):
    """The runs held by one candidate transcript: a subagent told to run the skill, or the invocations of a session."""
    if os.path.basename(os.path.dirname(path)) == "subagents":
        with open(path, errors="ignore") as handle:
            head = handle.readline()
        found = PROFILE.subagent_run and PROFILE.subagent_run.search(
            text_of((json.loads(head).get("message") or {}).get("content")))
        if not found:
            return []
        transcript = Transcript(path)
        report = find_report(transcript, transcript.start, transcript.end)
        end_reason = "report" if report else "transcript end, no report"
        return [build_run(SUBAGENT_KIND, transcript, session, transcript.start, transcript.end, end_reason,
                          "subagent", PROFILE.skill, transcript.first_prompt, report, project)]
    transcript = Transcript(path)
    return [build_run("interactive", transcript, session, start, end, end_reason, trigger, skill, args, report, project)
            for start, end, end_reason, trigger, skill, args, report in interactive_windows(transcript)]


def extract_session(session_dir, paths):
    """Every run held by the candidate transcripts of one session."""
    session = Session(session_dir)
    project = os.path.basename(os.path.dirname(session_dir))
    runs = []
    for path in sorted(paths):
        try:
            runs.extend(extract_runs(path, session, project))
        except (OSError, ValueError, KeyError) as error:
            print("skip %s: %s" % (path, error), file=sys.stderr)
    return runs


def scan(root, state_dir):
    """Extracts again only the sessions whose files changed since the last scan; the store holds the others."""
    started = time.monotonic()
    cache_path = os.path.join(state_dir, ".scan-cache.json")
    try:
        cache = read_json(cache_path)
    except (OSError, ValueError):
        cache = {}
    by_session = {}
    for path in candidate_files(root, cache):
        by_session.setdefault(session_dir_of(path), []).append(path)
    store = SessionStore(state_dir)
    extracted = 0
    for session_dir, paths in sorted(by_session.items()):
        session_id, fingerprint = os.path.basename(session_dir), session_fingerprint(session_dir)
        if store.is_current(session_id, fingerprint):
            continue
        header = {"id": session_id, "project": os.path.basename(os.path.dirname(session_dir)), "path": session_dir,
                  "fingerprint": fingerprint, "extractor": EXTRACTOR_VERSION, "profile": PROFILE.digest,
                  "extracted_at": iso(datetime.now(tz=timezone.utc).timestamp())}
        store.save(header, extract_session(session_dir, paths))
        extracted += 1
    os.makedirs(state_dir, exist_ok=True)
    write_json(cache_path, cache)
    return {
        "generated_at": iso(datetime.now(tz=timezone.utc).timestamp()), "root": root,
        "scan": {"sessions": len(by_session), "extracted": extracted, "stored": len(store.sessions),
                 "seconds": round(time.monotonic() - started, 2)},
        "profile": PROFILE.public(), "groups": [g for g, _ in GROUPS], "group_leaves": PROFILE.group_leaves,
        "phases": PROFILE.phases, "runs": store.runs(),
    }


def write_dashboard(data, state_dir):
    os.makedirs(state_dir, exist_ok=True)
    template = read_text(os.path.join(HERE, "dashboard.html"))
    payload = json.dumps(data).replace("</", "<\\/")
    html = template.replace("/*__DATA__*/null", payload)
    path = os.path.join(state_dir, "dashboard.html")
    with open(path, "w") as handle:
        handle.write(html)
    return path


def bind_server(port, handler):
    """The server on the first free port from the one asked: another dashboard may already hold it."""
    for candidate in range(port, port + PORT_ATTEMPTS) if port else [0]:
        try:
            return http.server.ThreadingHTTPServer(("127.0.0.1", candidate), handler)
        except OSError:
            continue
    raise OSError("no free port from %d to %d" % (port, port + PORT_ATTEMPTS - 1))


def serve(root, state_dir, port, open_browser):
    lock = threading.Lock()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.startswith("/data.json"):
                with lock:
                    data = scan(root, state_dir)
                    write_dashboard(data, state_dir)
                    body = json.dumps(data).encode()
                self._send(body, "application/json")
                return
            html = read_text(os.path.join(HERE, "dashboard.html")).replace("/*__DATA__*/null", "null")
            self._send(html.encode(), "text/html; charset=utf-8")

        def _send(self, body, content_type):
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = bind_server(port, Handler)
    url = "http://127.0.0.1:%d/" % server.server_address[1]
    print("Serving %s (Ctrl+C to stop)" % url)
    if open_browser:
        webbrowser.open(url)
    server.serve_forever()


def unattributed_runs(runs):
    """Runs whose causes do not add up to their duration: an attribution rule left a gap or an overlap."""
    return [r for r in runs if abs(sum(r["by_category"].values()) - r["duration"]) > MAX_UNATTRIBUTED_SECONDS]


def summary(data):
    runs = data["runs"]
    durations = sorted(r["duration"] / 60 for r in runs)
    if not durations:
        return "no run found"
    text = "%d runs · median %.1f min · max %.1f min · %d of %d sessions extracted in %.1f s" % (
        len(runs), statistics.median(durations), durations[-1], data["scan"]["extracted"], data["scan"]["sessions"],
        data["scan"]["seconds"])
    gaps = unattributed_runs(runs)
    if gaps:
        text += "\nWARNING causes do not sum to the duration of: " + ", ".join(r["id"] for r in gaps)
    return text


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["build", "serve", "calls", "steps"], nargs="?", default="build")
    parser.add_argument("transcript", nargs="?", help="calls, steps: a transcript path (Run spans → Transcript)")
    parser.add_argument("--cause", help="calls: keep one cause, e.g. 'build & test'")
    parser.add_argument("--root", default=DEFAULT_ROOT, help="Claude Code projects directory")
    parser.add_argument("--config", required=True, help="the profile of the observed skill (observe-<skill>/config.json)")
    parser.add_argument("--state", help="extraction history and dashboard, kept between scans (default: "
                                        "${XDG_STATE_HOME:-~/.local/state}/<profile name>)")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--open", action="store_true", help="open the dashboard in the browser")
    args = parser.parse_args()
    use_profile(load_profile(args.config))
    args.state = args.state or os.path.join(STATE_ROOT, PROFILE.name)
    if args.command == "steps":
        if not args.transcript:
            parser.error("steps needs a transcript path")
        for switch in step_switches(args.transcript):
            print("%s  %-40s via %-30r %s" % (switch["at"], switch["step"][:40], switch["marker"][:30], switch["signal"]))
        return
    if args.command == "calls":
        if not args.transcript:
            parser.error("calls needs a transcript path")
        for call in list_calls(args.transcript, args.cause):
            print("%s  %-12s %-24s %7.1f s %-6s %s" % (call["start"], call["actor"], call["category"], call["seconds"],
                                                      "FAILED" if call["failed"] else "", call["command"]))
        return
    if args.command == "serve":
        serve(args.root, args.state, args.port, args.open)
        return
    data = scan(args.root, args.state)
    path = write_dashboard(data, args.state)
    print("%s → %s" % (summary(data), path))
    if args.open:
        webbrowser.open("file://" + path)


if __name__ == "__main__":
    main()
