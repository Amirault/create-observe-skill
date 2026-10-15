#!/usr/bin/env python3
"""Collect the evidence needed to write the observe profile (config.json) of a skill.

Reads the local Claude Code transcripts and reports, for the skill and its former names:
how its runs start, which subagents they launch, how they end (headings, bold labels,
pull request links) and how long they last.

    python3 survey.py <skill> [--skill-md <path>] [--alias <former-name>]... [--samples N] [--root ~/.claude/projects]
"""

import argparse
import collections
import json
import os
import re
import statistics
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import observe  # noqa: E402

HEADING_RE = re.compile(r"(?m)^#{1,3}\s+(.+)$")
BOLD_LABEL_RE = re.compile(r"\*\*([A-Z][\w /→&-]{1,40}?):?\*\*:?")
VARIABLE_PART_RE = re.compile(r"[`\d#]+|\b\d{4}-\d{2}-\d{2}-[\w-]+")
SECTION_RE = re.compile(r"(?m)^(#{2,3} .+)$")
STEP_HEADING_RE = re.compile(r"^#{2,3} (?:(?:Step|Phase|Stage|Étape)\b|\d)", re.I)
FILE_MARKER_RE = re.compile(r"(?:references|scripts)/[\w.-]+\w")
COMMAND_MARKER_RE = re.compile(r"`([a-z][\w.-]*\s+-{0,2}[a-z][\w.-]*(?:\s+-{0,2}[\w.-]+)?)[^`]*`")


def former_names(skill, skill_md):
    """Names the skill folder had before a rename, from the git history of its SKILL.md."""
    folder = os.path.dirname(os.path.abspath(skill_md))
    try:
        log = subprocess.run(["git", "log", "--follow", "--name-only", "--format=", "--", "SKILL.md"], cwd=folder,
                             capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return []
    names = {path.split("/")[-2] for path in log.split() if path.endswith("/SKILL.md")}
    return sorted(names - {skill})


def shape(text):
    """A heading or label with its variable parts (slugs, numbers, code) blanked, to group similar ones."""
    return VARIABLE_PART_RE.sub("…", text).strip()[:80]


class Survey:
    def __init__(self, samples):
        self.samples = samples
        self.starts = collections.Counter()
        self.start_examples = collections.defaultdict(list)
        self.subagent_prompts = collections.Counter()
        self.agent_descriptions = collections.Counter()
        self.headings = collections.Counter()
        self.labels = collections.Counter()
        self.durations = []
        self.with_pull_request = 0
        self.final_examples = []
        self.run_signals = []
        self.steps = []

    def read_subagent(self, path):
        transcript = observe.Transcript(path)
        prompt = transcript.first_prompt
        for name in observe.PROFILE.names:
            found = re.search(r"[^.\n]*\b%s/SKILL\.md[^.\n]*" % re.escape(name), prompt)
            if found:
                self.subagent_prompts[shape(found.group(0))] += 1
                return

    def read_session(self, path):
        transcript = observe.Transcript(path)
        for start, end, end_reason, trigger, skill, args, _ in observe.interactive_windows(transcript):
            self.starts[trigger] += 1
            if len(self.start_examples[trigger]) < self.samples:
                self.start_examples[trigger].append("%s %s" % (observe.iso(start), re.sub(r"\s+", " ", args)[:140]))
            self.durations.append((end - start) / 60)
            for tool in transcript.tools.values():
                if tool["name"] in observe.AGENT_TOOLS and start <= tool["t0"] <= end:
                    self.agent_descriptions[shape(tool["input"].get("description", "?"))] += 1
            self.read_final_message(observe.final_message(transcript, start, end), end_reason)
            signals = [observe.signal_of(tool) for tool in transcript.tools.values() if start <= tool["t0"] <= end]
            signals += [e["text"] for e in transcript.events if e["kind"] == "assistant" and start <= e["t"] <= end]
            self.run_signals.append("\n".join(signals))

    def read_steps(self, skill_md_text):
        """The steps of the observed skill, each with the files and commands it names: runs where seen, other steps naming them."""
        parts = SECTION_RE.split(skill_md_text)
        sections = [(h.lstrip("# ").strip(), body) for h, body in zip(parts[1::2], parts[2::2]) if STEP_HEADING_RE.match(h)]
        named = [(step, list(dict.fromkeys(FILE_MARKER_RE.findall(body) + COMMAND_MARKER_RE.findall(body))))
                 for step, body in sections]
        self.steps = [{"step": step, "candidates": [
            {"marker": marker, "runs": sum(1 for signals in self.run_signals if marker in signals),
             "shared": [other for other, markers in named if other != step and marker in markers]}
            for marker in markers]} for step, markers in named]

    def read_final_message(self, text, end_reason):
        self.headings.update({shape(h) for h in HEADING_RE.findall(text)})
        self.labels.update({label.strip() for label in BOLD_LABEL_RE.findall(text)})
        self.with_pull_request += bool(observe.PR_REFERENCE_RE.search(text))
        if len(self.final_examples) < self.samples:
            self.final_examples.append("[%s] %s" % (end_reason, re.sub(r"\s+", " ", text)[:300]))

    def report(self, skill, aliases):
        runs = sum(self.starts.values())
        lines = ["# Survey of %s (aliases: %s)" % (skill, ", ".join(aliases) or "none"), "",
                 "## Starts in interactive sessions: %d" % runs]
        for trigger, count in self.starts.most_common():
            lines.append("- %s: %d" % (trigger, count))
            lines.extend("    %s" % example for example in self.start_examples[trigger])
        if self.durations:
            lines.append("- duration until the next human prompt: median %.1f min, max %.1f min" % (
                statistics.median(self.durations), max(self.durations)))
        lines += ["", "## Subagents told to read the skill (candidate subagent_run): %d" % sum(self.subagent_prompts.values())]
        lines.extend("- %d × %s" % (n, p) for p, n in self.subagent_prompts.most_common(self.samples))
        lines += ["", "## Agent descriptions inside the runs (candidate phases)"]
        lines.extend("- %d × %s" % (n, d) for d, n in self.agent_descriptions.most_common(25))
        lines += ["", "## Final messages: %d of %d link a pull request" % (self.with_pull_request, runs),
                  "Headings (candidate report):"]
        lines.extend("- %d × %s" % (n, h) for h, n in self.headings.most_common(10))
        lines.append("Bold labels (candidate outcome line):")
        lines.extend("- %d × %s" % (n, b) for b, n in self.labels.most_common(15))
        lines.append("Examples:")
        lines.extend("- %s" % example for example in self.final_examples)
        lines += ["", "## Steps of the skill (candidate markers: runs where seen of %d; ⚠ named by another step)" % runs]
        if not self.steps:
            lines.append("- no `## Step …` heading found: name the steps from the skill's own sections")
        for step in self.steps:
            lines.append("- %s" % step["step"])
            lines.extend("    %s %s: %d runs%s" % ("⚠" if c["shared"] else "·", c["marker"], c["runs"],
                                                 " — also in " + ", ".join(c["shared"]) if c["shared"] else "")
                         for c in step["candidates"])
            if not step["candidates"]:
                lines.append("    no file or command named: look for its subagents or its words in the agent text")
        return "\n".join(lines)


def run_survey(root, skill, aliases, samples, skill_md=None):
    observe.use_profile(observe.Profile({"name": "survey", "skill": skill, "aliases": aliases}))
    survey = Survey(samples)
    for directory, _, names in os.walk(root):
        for name in names:
            path = os.path.join(directory, name)
            if not name.endswith(".jsonl"):
                continue
            with open(path, errors="ignore") as handle:
                if not observe.PROFILE.mentioned_in(handle.read()):
                    continue
            try:
                if os.path.basename(directory) == "subagents":
                    survey.read_subagent(path)
                    continue
                survey.read_session(path)
            except (OSError, ValueError, KeyError) as error:
                print("skip %s: %s" % (path, error), file=sys.stderr)
    if skill_md and os.path.exists(skill_md):
        with open(skill_md) as handle:
            survey.read_steps(handle.read())
    return survey


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("skill")
    parser.add_argument("--skill-md", help="the observed SKILL.md (default: <skills folder>/<skill>/SKILL.md)")
    parser.add_argument("--alias", action="append", default=None, help="a former name (default: from git history)")
    parser.add_argument("--samples", type=int, default=3)
    parser.add_argument("--root", default=observe.DEFAULT_ROOT)
    args = parser.parse_args()
    skill_md = args.skill_md or os.path.join(HERE, "..", "..", args.skill, "SKILL.md")
    aliases = args.alias if args.alias is not None else former_names(args.skill, skill_md)
    print(run_survey(args.root, args.skill, aliases, args.samples, skill_md).report(args.skill, aliases))
    print("\n## Profile skeleton\n" + json.dumps({"name": "observe-" + args.skill, "skill": args.skill,
                                                  "aliases": aliases}, indent=2))


if __name__ == "__main__":
    main()
