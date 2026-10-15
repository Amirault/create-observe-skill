"""Tests for survey.py: the evidence it collects to write an observe profile."""

import json
import os
import tempfile
import unittest

import survey


def write_jsonl(path, records):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")


def at(second):
    return "2026-10-01T10:%02d:%02d.000Z" % divmod(second, 60)


def tidy_code_session(root):
    write_jsonl(os.path.join(root, "project", "session.jsonl"), [
        {"type": "user", "timestamp": at(0), "message": {"role": "user", "content": "/tidy-code first 2 files"}},
        {"type": "assistant", "timestamp": at(10), "message": {"id": "a1", "role": "assistant", "content": [
            {"type": "tool_use", "id": "t1", "name": "Agent", "input": {"description": "Triage files 0-81"}}]}},
        {"type": "user", "timestamp": at(20), "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "done"}]}},
        {"type": "assistant", "timestamp": at(25), "message": {"id": "a3", "role": "assistant", "content": [
            {"type": "tool_use", "id": "t2", "name": "Bash", "input": {"command": "bash scripts/fix.sh Foo.cs"}}]}},
        {"type": "user", "timestamp": at(28), "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t2", "content": "ok"}]}},
        {"type": "assistant", "timestamp": at(30), "message": {"id": "a2", "role": "assistant", "content": [
            {"type": "text", "text": "**Checks:** green. Opened https://example.com/org/repo/pull/12."}]}},
        {"type": "user", "timestamp": at(90), "message": {"role": "user", "content": "thanks"}},
    ])


SKILL_MD = """# Tidy

## Step 1 — Fetch

Read `references/scan-rules.md`, then run `scripts/fetch.sh`.

## Step 2 — Fix

Run `scripts/fetch.sh` again, then `scripts/fix.sh <file>`.
"""


class SubagentPromptTest(unittest.TestCase):
    def test_subagent_told_to_read_the_skill_counts_once_even_when_a_former_name_is_inside_the_name(self):
        # Given
        with tempfile.TemporaryDirectory() as root:
            write_jsonl(os.path.join(root, "project", "session", "subagents", "agent-1.jsonl"), [
                {"type": "user", "timestamp": at(0), "message": {"role": "user", "content":
                    "Read `skills/new-tidy-code/SKILL.md` and run it on the repository."}}])

            # When
            found = survey.run_survey(root, "new-tidy-code", ["tidy-code"], 3)

        # Then
        self.assertEqual(sum(found.subagent_prompts.values()), 1)


class SurveyTest(unittest.TestCase):
    def test_steps_come_with_their_candidate_markers_runs_seen_and_sharing(self):
        # Given
        with tempfile.TemporaryDirectory() as root:
            tidy_code_session(root)
            skill_md = os.path.join(root, "SKILL.md")
            with open(skill_md, "w") as handle:
                handle.write(SKILL_MD)

            # When
            found = survey.run_survey(root, "tidy-code", [], 3, skill_md)

        # Then
        self.assertEqual(found.steps, [
            {"step": "Step 1 — Fetch", "candidates": [
                {"marker": "references/scan-rules.md", "runs": 0, "shared": []},
                {"marker": "scripts/fetch.sh", "runs": 0, "shared": ["Step 2 — Fix"]}]},
            {"step": "Step 2 — Fix", "candidates": [
                {"marker": "scripts/fetch.sh", "runs": 0, "shared": ["Step 1 — Fetch"]},
                {"marker": "scripts/fix.sh", "runs": 1, "shared": []}]}])

    def test_survey_reports_starts_phases_and_how_runs_end(self):
        # Given
        with tempfile.TemporaryDirectory() as root:
            tidy_code_session(root)

            # When
            found = survey.run_survey(root, "tidy-code", [], 3)

        # Then
        self.assertEqual(dict(found.starts), {"invocation": 1})
        self.assertEqual(dict(found.agent_descriptions), {"Triage files …-…": 1})
        self.assertEqual((found.with_pull_request, dict(found.labels)), (1, {"Checks": 1}))


if __name__ == "__main__":
    unittest.main()
