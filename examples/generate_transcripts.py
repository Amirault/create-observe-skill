#!/usr/bin/env python3
"""Write fictional Claude Code transcripts of the `tidy-code` skill, for the demo and the CI.

Each session is one `/tidy-code` run in a made-up `acme-shop` project: a triage subagent,
`lintkit --fix`, the tests, a commit, a review subagent and a pull request. Most runs open a
pull request, some abort on red tests, some stop to ask a question. The last runs are faster
(the test suite got quicker), so the dashboard has something to say. Seeded: the same
transcripts every time.

    python3 generate_transcripts.py <projects-root> [--runs 32]
"""

import argparse
import json
import os
import random
from datetime import datetime, timedelta, timezone

PROJECT = "-home-dev-acme-shop"
FIRST_DAY = datetime(2026, 8, 3, 9, 0, 0, tzinfo=timezone.utc)
FILES = ["src/cart.py", "src/checkout.py", "src/discounts.py", "src/inventory.py", "src/shipping.py",
         "src/invoice.py", "src/catalog.py", "src/coupons.py"]


class Session:
    """Builds one session transcript and its subagent transcripts, second by second."""

    def __init__(self, root, index, start):
        self.root, self.index, self.start = root, index, start
        self.id = "5e55%04d-0000-4000-8000-%012d" % (index, index)
        self.now = 0.0
        self.records, self.counter = [], 0

    def at(self, seconds=None):
        return (self.start + timedelta(seconds=self.now if seconds is None else seconds)).strftime(
            "%Y-%m-%dT%H:%M:%S.000Z")

    def next_id(self, prefix):
        self.counter += 1
        return "%s%04d%03d" % (prefix, self.index, self.counter)

    def user(self, content, records=None):
        (self.records if records is None else records).append(
            {"type": "user", "timestamp": self.at(), "message": {"role": "user", "content": content}})

    def assistant(self, blocks, records=None):
        message = {"id": self.next_id("msg_"), "model": "demo-model", "role": "assistant", "content": blocks,
                   "usage": {"input_tokens": 1, "output_tokens": 50}}
        (self.records if records is None else records).append(
            {"type": "assistant", "timestamp": self.at(), "message": message})

    def think(self, seconds):
        self.now += seconds

    def tool(self, name, seconds, result="ok", records=None, **tool_input):
        tool_id = self.next_id("toolu_")
        self.assistant([{"type": "tool_use", "id": tool_id, "name": name, "input": tool_input}], records)
        self.now += seconds
        self.user([{"type": "tool_result", "tool_use_id": tool_id, "content": result, "is_error": False}], records)
        return tool_id

    def text(self, value, records=None):
        self.assistant([{"type": "text", "text": value}], records)

    def subagent(self, description, prompt, work):
        """A foreground Agent call: `work` fills the subagent transcript and advances the clock."""
        self.counter += 1
        agent_id = "%06x%06x" % (self.index, self.counter)
        tool_id = self.next_id("toolu_")
        self.assistant([{"type": "tool_use", "id": tool_id, "name": "Agent",
                         "input": {"description": description, "prompt": prompt}}])
        child = []
        self.now += 1
        self.user(prompt, child)
        work(child)
        self.now += 1
        self.user([{"type": "tool_result", "tool_use_id": tool_id, "content": "done\nagentId: " + agent_id,
                    "is_error": False}])
        folder = os.path.join(self.root, PROJECT, self.id, "subagents")
        write_jsonl(os.path.join(folder, "agent-%s.jsonl" % agent_id), child)
        with open(os.path.join(folder, "agent-%s.meta.json" % agent_id), "w") as handle:
            json.dump({"agentType": "general-purpose", "description": description, "toolUseId": tool_id}, handle)

    def save(self):
        write_jsonl(os.path.join(self.root, PROJECT, self.id + ".jsonl"), self.records)


def write_jsonl(path, records):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")


def tidy_code_run(root, index, total, rng):
    start = FIRST_DAY + timedelta(days=index * 42 / total, hours=rng.uniform(0, 8))
    session = Session(root, index, start)
    count = rng.choice([1, 1, 2, 2, 3, 4])
    files = rng.sample(FILES, count)
    faster = index >= total * 0.6
    fate = rng.choices(["pr", "aborted", "asked"], [0.75, 0.15, 0.10])[0]

    session.user("/tidy-code first %d file%s" % (count, "s" if count > 1 else ""))
    session.think(rng.uniform(6, 15))

    def triage(child):
        session.think(rng.uniform(5, 12))
        session.tool("Read", rng.uniform(1, 2), records=child,
                     file_path="/home/dev/acme-shop/.claude/skills/tidy-code/references/picking-rules.md")
        session.think(rng.uniform(4, 10))
        session.tool("Bash", rng.uniform(2, 6), "\n".join(FILES), records=child, command="git ls-files 'src/*.py'")
        session.think(rng.uniform(30, 90))
        session.text("Picked: " + ", ".join(files), child)

    session.subagent("Triage the candidate files", "Pick the %d files to tidy." % count, triage)
    session.think(rng.uniform(8, 20))
    session.text("Step 2 — Clean " + ", ".join(files))
    for name in files:
        session.think(rng.uniform(4, 10))
        session.tool("Bash", rng.uniform(20, 70), command="lintkit --fix " + name)
        session.think(rng.uniform(15, 45))
        session.tool("Edit", 1, file_path="/home/dev/acme-shop/" + name, old_string="x", new_string="y")
    session.think(rng.uniform(5, 12))
    session.tool("Bash", rng.uniform(40, 90) if faster else rng.uniform(150, 300), command="pytest -q")
    if fate == "aborted":
        session.think(rng.uniform(10, 25))
        session.text("## Tidy report\n\n**Outcome**: ABORTED, the tests went red after the fixes.")
        session.save()
        return
    session.think(rng.uniform(5, 12))
    session.tool("Bash", rng.uniform(25, 60) if faster else rng.uniform(60, 140),
                 command='git add -A && git commit -m "refactor: tidy %d files"' % count)
    if fate == "asked":
        session.think(rng.uniform(10, 25))
        session.text("The commit is ready, but `src/coupons.py` changes a public signature. "
                     "Do you want me to open the pull request anyway?")
        session.think(rng.uniform(300, 900))
        session.user("no, leave it")
        session.save()
        return

    def review(child):
        session.think(rng.uniform(8, 15))
        for name in files:
            session.tool("Read", 1, records=child, file_path="/home/dev/acme-shop/" + name)
            session.think(rng.uniform(20, 60))
        session.tool("Bash", rng.uniform(1, 3), records=child, command="git diff HEAD~1 --stat")
        session.think(rng.uniform(30, 80))
        session.text("No finding.", child)

    session.subagent("Review the tidy diff", "Review the last commit, cold.", review)
    session.think(rng.uniform(5, 12))
    session.tool("Bash", rng.uniform(3, 8), "https://example.com/acme/shop/pull/%d" % (100 + index),
                 command="git push -u origin HEAD && gh pr create --fill")
    session.think(rng.uniform(8, 20))
    session.text("## Tidy report\n\n**Outcome**: PR OPENED\n\nhttps://example.com/acme/shop/pull/%d" % (100 + index))
    session.think(rng.uniform(120, 600))
    session.user("thanks")
    session.save()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("root", help="where to write the transcripts (a stand-in for ~/.claude/projects)")
    parser.add_argument("--runs", type=int, default=32)
    args = parser.parse_args()
    rng = random.Random(7)
    for index in range(args.runs):
        tidy_code_run(args.root, index, args.runs, rng)
    print("wrote %d tidy-code sessions under %s" % (args.runs, os.path.join(args.root, PROJECT)))


if __name__ == "__main__":
    main()
