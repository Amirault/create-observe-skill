"""Tests for observe.py: run detection, time attribution, command and outcome classification, profiles.

The scan tests write throwaway Claude Code transcripts (a session JSONL, its `subagents/` folder
and their `.meta.json`) under a temporary projects root, then run the real `scan` against a
temporary state folder.
"""

import http.server
import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import observe

DELIVERY = observe.Profile(json.loads(r'''{"name": "observe-ship-change", "skill": "ship-change", "aliases": ["old-ship-change"], "subagent_run": "(?:old-)?ship-change/SKILL\\.md`?,?\\s+and\\s+run\\s+it", "subagent_label": "Replay", "subagent_spec": "on spec `([\\w.-]+)`", "report": "(?im)^#{1,3}\\s.*delivery report", "report_header": "(?i)delivery report\\s*[—–:-]+\\s*`?([\\w.-]+?)`?,?\\s+increment\\s+(\\d+)(?:\\s+of\\s+(\\d+))?", "spec": "(\\d{4}-\\d{2}-\\d{2}-[a-z0-9][a-z0-9-]+?)(?:\\.md)?(?=[\\s`'\\\",.)]|$)", "outcome_keywords": [["SPEC IMPLEMENTED", "SPEC IMPLEMENTED"], ["PR OPENED", "PR OPENED"], ["PULL REQUEST OPENED", "PR OPENED"], ["ABORTED", "ABORTED"], ["SUCCESS", "SUCCESS"]], "delivered": ["SPEC IMPLEMENTED", "PR OPENED", "SUCCESS"], "phases": {"implement": ["implement"], "refactor": ["refactor"], "review": ["review"]}, "commands": [{"cause": "hook run by hand", "pattern": "\\bhookrunner\\s+run\\b", "group": "Pre-commit suite"}]}'''))
MINIMAL = observe.Profile({"name": "observe-tidy-code", "skill": "tidy-code",
                           "phases": {"review": ["review"], "triage": ["triage"]}})
T0 = datetime(2026, 10, 1, 10, 0, 0, tzinfo=timezone.utc)
CHILD_ID = "abc123def456"
REPLAY_PROMPT = (
    "Read `skills/ship-change/SKILL.md` and run it, unchanged, "
    "on spec `add-discount-rule-20261001100000`, one increment."
)


def at(seconds):
    return (T0 + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def user(seconds, content, **extra):
    return dict({"type": "user", "timestamp": at(seconds), "message": {"role": "user", "content": content}}, **extra)


def assistant(seconds, message_id, *blocks):
    message = {"id": message_id, "model": "test-model", "role": "assistant", "content": list(blocks),
               "usage": {"input_tokens": 1, "output_tokens": 10}}
    return {"type": "assistant", "timestamp": at(seconds), "message": message}


def tool_use(tool_id, name, **tool_input):
    return {"type": "tool_use", "id": tool_id, "name": name, "input": tool_input}


def text(value):
    return {"type": "text", "text": value}


def tool_result(seconds, tool_id, content="ok"):
    return user(seconds, [{"type": "tool_result", "tool_use_id": tool_id, "content": content, "is_error": False}])


def write_jsonl(path, records):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")


def interactive_session(root):
    """A /ship-change run: git status, a foreground implement subagent, a commit, a report."""
    session = os.path.join(root, "project", "session-1")
    write_jsonl(session + ".jsonl", [
        user(0, "/ship-change 2026-10-01-quote-reference.md"),
        assistant(10, "m1", tool_use("tu1", "Bash", command="git status --porcelain")),
        tool_result(11, "tu1"),
        assistant(20, "m2", tool_use("tu2", "Agent", description="implement(1)", prompt="Implement increment 1")),
        tool_result(82, "tu2", "STATUS: DONE\nagentId: " + CHILD_ID),
        assistant(90, "m3", tool_use("tu3", "Bash", command='git add -A && git commit -m "feat: trim"')),
        tool_result(150, "tu3"),
        assistant(160, "m4", text("# Delivery Report — 2026-10-01-quote-reference, increment 1 of 2\n\n"
                                  "**Outcome**: PR OPENED\n**Duration**: 2m40s")),
        user(300, "thanks"),
    ])
    child = os.path.join(session, "subagents", "agent-" + CHILD_ID + ".jsonl")
    write_jsonl(child, [
        user(21, "Implement increment 1"),
        assistant(30, "c1", tool_use("ct1", "Bash", command="npm test")),
        tool_result(70, "ct1"),
        assistant(81, "c2", text("STATUS: DONE")),
    ])
    with open(child[:-len(".jsonl")] + ".meta.json", "w") as handle:
        json.dump({"agentType": "general-purpose", "description": "implement(1)", "toolUseId": "tu2"}, handle)
    return session + ".jsonl"


def replay_returning_early(root):
    """A replay subagent that ends its turn early and resumes only when the coordinator nudges it."""
    write_jsonl(os.path.join(root, "project", "session-2", "subagents", "agent-fedcba987654.jsonl"), [
        user(0, REPLAY_PROMPT),
        assistant(5, "r1", text("The implement subagent is running in the background.")),
        user(1000, "The coordinator sent a message while you were working:\nResume.", isMeta=True,
             origin={"kind": "coordinator"}),
        assistant(1005, "r2", text("# Delivery Report — add-discount-rule-20261001100000, "
                                   "increment 1 of 1\n\n**Outcome**: SPEC IMPLEMENTED")),
    ])


def session_mentioning_the_skill_without_running_it(root):
    write_jsonl(os.path.join(root, "project", "session-3.jsonl"), [
        user(0, "How long does ship-change take on average?"),
        assistant(5, "q1", text("About 15 minutes.")),
    ])


def stored_session_of_an_older_extractor(state_dir):
    """A session extracted by extractor 1, whose transcript Claude Code has pruned since."""
    gone = {"id": "gone@2026-09-01T10:00:00Z", "transcript": "/nowhere/gone/subagents/agent-gone.jsonl",
            "start": "2026-09-01T10:00:00Z", "outcome": "SPEC IMPLEMENTED", "llm_turns": 999,
            "by_category": {}, "by_group": {}, "by_actor_group": {}}
    write_jsonl(os.path.join(state_dir, "sessions", "gone.jsonl"), [
        {"session": {"id": "gone", "path": "/nowhere/gone", "fingerprint": "f", "extractor": 1, "runs": 1}},
        {"run": gone},
    ])


def cause_of(command):
    return observe.classify_tool("Bash", {"command": command})


class ScanTestCase(unittest.TestCase):
    profile = DELIVERY

    def setUp(self):
        observe.use_profile(self.profile)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = os.path.join(self._tmp.name, "projects")
        self.state = os.path.join(self._tmp.name, "state")

    def scan(self):
        return observe.scan(self.root, self.state)

    def only_run(self):
        return self.scan()["runs"][0]

    def stored(self, session_id):
        with open(os.path.join(self.state, "sessions", session_id + ".jsonl")) as handle:
            return [json.loads(line) for line in handle]

    def spy_on_extraction(self):
        patcher = mock.patch.object(observe, "extract_session", wraps=observe.extract_session)
        spy = patcher.start()
        self.addCleanup(patcher.stop)
        return spy


class InteractiveRunTest(ScanTestCase):
    def test_run_spans_from_the_invocation_to_its_report(self):
        # Given
        interactive_session(self.root)

        # When
        run = self.only_run()

        # Then
        self.assertEqual((run["duration"], run["end_reason"]), (160, "report"))

    def test_every_second_of_the_run_gets_exactly_one_cause(self):
        # Given
        interactive_session(self.root)

        # When
        run = self.only_run()

        # Then
        self.assertAlmostEqual(sum(run["by_category"].values()), run["duration"], places=1)

    def test_foreground_subagent_wait_is_replaced_by_what_the_subagent_did(self):
        # Given
        interactive_session(self.root)

        # When
        run = self.only_run()

        # Then
        self.assertEqual(run["by_actor"], {"orchestrator": 100, "implement": 60})
        self.assertEqual((run["by_category"]["build & test"], run["by_category"]["agent overhead"]), (40, 2))

    def test_git_commit_counts_as_one_pre_commit_suite_execution(self):
        # Given
        interactive_session(self.root)

        # When
        run = self.only_run()

        # Then
        self.assertEqual(run["by_group"]["Pre-commit suite"], 60)
        self.assertEqual((run["commits"]["executions"], run["commits"]["by_actor"]), (1, {"orchestrator": 1}))

    def test_report_gives_outcome_increment_spec_and_self_reported_duration(self):
        # Given
        interactive_session(self.root)

        # When
        run = self.only_run()

        # Then
        self.assertEqual((run["outcome"], run["delivered"], run["spec"]), ("PR OPENED", True, "2026-10-01-quote-reference"))
        self.assertEqual((run["increment"], run["increments_total"], run["reported_duration"]), (1, 2, "2m40s"))

    def test_list_calls_returns_every_tool_call_of_the_run_tree_oldest_first(self):
        # Given
        transcript = interactive_session(self.root)

        # When
        calls = observe.list_calls(transcript)

        # Then
        self.assertEqual([(c["actor"], c["category"], c["seconds"]) for c in calls], [
            ("orchestrator", "git local", 1), ("implement", "build & test", 40),
            ("orchestrator", observe.GIT_COMMIT, 60)])

    def test_list_calls_keeps_only_the_cause_asked_for(self):
        # Given
        transcript = interactive_session(self.root)

        # When
        calls = observe.list_calls(transcript, observe.GIT_COMMIT)

        # Then
        self.assertEqual([c["command"] for c in calls], ['git add -A && git commit -m "feat: trim"'])


def tidy_code_session(root):
    """A /tidy-code run without report: a review subagent, then a final message linking the PR."""
    session = os.path.join(root, "project", "session-4")
    write_jsonl(session + ".jsonl", [
        user(0, "<command-message>tidy-code</command-message>\n<command-name>/tidy-code</command-name>"),
        assistant(10, "f1", tool_use("fs1", "Bash", command="npm test -- foo.test.js")),
        tool_result(130, "fs1"),
        assistant(140, "f2", tool_use("fa1", "Agent", description="Independent review", prompt="Review")),
        tool_result(200, "fa1", "PASS\nagentId: 0123456789ab"),
        assistant(202, "f4", tool_use("fp1", "Bash", command="gh pr create --fill")),
        tool_result(205, "fp1", "https://example.com/org/repo/pull/4000"),
        assistant(210, "f3", text("Opened https://example.com/org/repo/pull/4000 for the cleanup.")),
        user(900, "merci"),
    ])
    write_jsonl(os.path.join(session, "subagents", "agent-0123456789ab.jsonl"), [
        user(141, "Independently review the change"),
        assistant(199, "v1", text("PASS")),
    ])
    with open(os.path.join(session, "subagents", "agent-0123456789ab.meta.json"), "w") as handle:
        json.dump({"description": "Independent review", "toolUseId": "fa1"}, handle)


class GenericProfileTest(ScanTestCase):
    profile = MINIMAL

    def test_run_without_report_ends_at_its_last_message_before_the_next_human_prompt(self):
        # Given
        tidy_code_session(self.root)

        # When
        run = self.only_run()

        # Then
        self.assertEqual((run["duration"], run["end_reason"]), (210, "last message before a human prompt"))

    def test_final_message_linking_a_pull_request_makes_the_run_delivered(self):
        # Given
        tidy_code_session(self.root)

        # When
        run = self.only_run()

        # Then
        self.assertEqual((run["outcome"], run["outcome_inferred"], run["delivered"]), ("PR OPENED", True, True))

    def test_subagents_get_the_phases_of_the_profile(self):
        # Given
        tidy_code_session(self.root)

        # When
        run = self.only_run()

        # Then
        self.assertEqual((run["by_actor"]["review"], run["by_category"]["build & test"]), (58, 120))

    def test_final_message_matching_a_final_outcome_gets_its_label(self):
        # Given
        observe.use_profile(observe.Profile(dict(MINIMAL.config, final_outcomes=[["(?i)let me know", "ASKED HUMAN"]])))
        write_jsonl(os.path.join(self.root, "project", "session-6.jsonl"), [
            user(0, "/tidy-code"),
            assistant(60, "e1", text("Standing by: let me know which file to clean next.")),
            user(500, "the first one"),
        ])

        # When
        run = self.only_run()

        # Then
        self.assertEqual((run["outcome"], run["delivered"]), ("ASKED HUMAN", False))

    def test_run_quoting_a_pull_request_it_did_not_create_is_not_delivered(self):
        # Given
        write_jsonl(os.path.join(self.root, "project", "session-8.jsonl"), [
            user(0, "/tidy-code"),
            assistant(14, "q1", text("Stopped before touching anything: the branch of https://example.com/org/repo/pull/12 is busy.")),
            user(600, "ok"),
        ])

        # When
        run = self.only_run()

        # Then
        self.assertEqual((run["outcome"], run["delivered"]), ("NO REPORT", False))

    def test_pull_request_created_on_a_later_line_of_a_command_makes_the_run_delivered(self):
        # Given
        write_jsonl(os.path.join(self.root, "project", "session-9.jsonl"), [
            user(0, "/tidy-code"),
            assistant(5, "w1", tool_use("wp1", "Bash", command="cat > body.md <<'EOF'\nCleanup\nEOF\nGH=gh; \"$GH\" pr create --body-file body.md")),
            tool_result(9, "wp1", "https://example.com/org/repo/pull/13"),
            assistant(12, "w2", text("I opened https://example.com/org/repo/pull/13.")),
            user(600, "ok"),
        ])

        # When
        run = self.only_run()

        # Then
        self.assertEqual((run["outcome"], run["delivered"]), ("PR OPENED", True))

    def test_invocation_without_arguments_gets_the_default_spec(self):
        # Given
        observe.use_profile(observe.Profile(dict(MINIMAL.config, spec=r"(\d+)\s*targets?", spec_default="1")))
        tidy_code_session(self.root)

        # When
        run = self.only_run()

        # Then
        self.assertEqual(run["spec"], "1")

    def test_skill_tool_call_right_after_the_slash_command_is_the_same_run(self):
        # Given
        write_jsonl(os.path.join(self.root, "project", "session-5.jsonl"), [
            user(0, "/tidy-code"),
            assistant(5, "d1", text("The skill just got picked up. Invoking it now."), tool_use("ds1", "Skill", skill="tidy-code")),
            tool_result(6, "ds1", "Launching skill: tidy-code"),
            assistant(300, "d2", text("Opened https://example.com/org/repo/pull/4001.")),
        ])

        # When
        runs = self.scan()["runs"]

        # Then
        self.assertEqual([(r["trigger"], r["duration"]) for r in runs], [("invocation", 300)])

    def test_profile_change_extracts_every_session_again(self):
        # Given
        tidy_code_session(self.root)
        self.scan()
        observe.use_profile(observe.Profile(dict(MINIMAL.config, title="Tidy code")))
        extraction = self.spy_on_extraction()

        # When
        self.scan()

        # Then
        self.assertEqual(extraction.call_count, 1)


STEPPED = observe.Profile(dict(MINIMAL.config, steps=[
    {"name": "Step 0 Scope", "markers": []},
    {"name": "Step 3 Find the test", "markers": ["^Bash: .*npm test"]},
    {"name": "Step 6 Review", "markers": ["^Agent: .*review"]},
    {"name": "Step 7 PR", "markers": ["^Bash: .*gh pr create", "^Text: .*/pull/\\d+"]},
    {"name": "Step 9 Never run", "markers": ["^Bash: .*never-called"]},
]))


class StepTest(ScanTestCase):
    profile = STEPPED

    def test_each_second_belongs_to_the_step_entered_by_the_last_marker(self):
        # Given
        tidy_code_session(self.root)

        # When
        run = self.only_run()

        # Then
        self.assertEqual(run["by_step"], {"Step 0 Scope": 10, "Step 3 Find the test": 130, "Step 6 Review": 62, "Step 7 PR": 8})
        self.assertEqual([s[2] for s in run["steps"]], ["Step 0 Scope", "Step 3 Find the test", "Step 6 Review", "Step 7 PR"])

    def test_run_lists_the_steps_its_markers_revealed(self):
        # Given
        tidy_code_session(self.root)

        # When
        run = self.only_run()

        # Then
        self.assertEqual(run["steps_seen"], ["Step 3 Find the test", "Step 6 Review", "Step 7 PR"])

    def test_step_time_is_split_by_cause_group(self):
        # Given
        tidy_code_session(self.root)

        # When
        run = self.only_run()

        # Then
        self.assertEqual(run["by_step_group"]["Step 3 Find the test"], {"Build & test": 120, "LLM": 10})

    def test_step_switches_name_the_marker_and_the_signal_behind_each_switch(self):
        # Given
        transcript = os.path.join(self.root, "project", "session-4.jsonl")
        tidy_code_session(self.root)

        # When
        switches = observe.step_switches(transcript)

        # Then
        self.assertEqual([(s["offset"], s["step"], s["marker"]) for s in switches], [
            (10, "Step 3 Find the test", "^Bash: .*npm test"), (140, "Step 6 Review", "^Agent: .*review"),
            (202, "Step 7 PR", "^Bash: .*gh pr create")])

    def test_scan_exposes_every_step_of_the_profile_in_order(self):
        # Given
        tidy_code_session(self.root)

        # When
        data = self.scan()

        # Then
        self.assertEqual(data["profile"]["steps"][-1], "Step 9 Never run")


class ServerTest(unittest.TestCase):
    def test_busy_port_moves_the_server_to_the_next_free_one(self):
        # Given
        busy = observe.bind_server(0, http.server.BaseHTTPRequestHandler)
        self.addCleanup(busy.server_close)
        port = busy.server_address[1]

        # When
        server = observe.bind_server(port, http.server.BaseHTTPRequestHandler)
        self.addCleanup(server.server_close)

        # Then
        self.assertNotEqual(server.server_address[1], port)


class SummaryTest(unittest.TestCase):
    def test_run_whose_causes_do_not_sum_to_its_duration_is_reported(self):
        # Given
        runs = [{"id": "gap@2026-10-01T10:00:00Z", "duration": 160, "by_category": {"LLM": 100}},
                {"id": "full@2026-10-01T11:00:00Z", "duration": 60, "by_category": {"LLM": 60}}]

        # When
        gaps = observe.unattributed_runs(runs)

        # Then
        self.assertEqual([r["id"] for r in gaps], ["gap@2026-10-01T10:00:00Z"])


class ContinuationTest(ScanTestCase):
    def test_continue_sent_after_a_system_reminder_starts_the_next_run(self):
        # Given
        interactive_session(self.root)
        with open(os.path.join(self.root, "project", "session-1.jsonl"), "a") as handle:
            for record in [user(400, "<system-reminder>\nYou are in a worktree.\n</system-reminder>\ncontinue"),
                           assistant(460, "m5", text("# Delivery Report — 2026-10-01-quote-reference, increment 2 of 2\n\n**Outcome**: PR OPENED"))]:
                handle.write(json.dumps(record) + "\n")

        # When
        runs = self.scan()["runs"]

        # Then
        self.assertEqual([(r["trigger"], r["increment"]) for r in runs], [("invocation", 1), ("continue", 2)])


class RetryTest(ScanTestCase):
    profile = MINIMAL

    def test_retry_after_a_run_cut_by_an_error_resumes_it_as_a_continue_run(self):
        # Given
        write_jsonl(os.path.join(self.root, "project", "session-7.jsonl"), [
            user(0, "/tidy-code"),
            assistant(50, "r1", text("API Error: getaddrinfo ENOTFOUND")),
            user(4000, "Réessayer"),
            assistant(4050, "r3", tool_use("rp1", "Bash", command="gh pr create --fill")),
            tool_result(4060, "rp1", "https://example.com/org/repo/pull/7"),
            assistant(4100, "r2", text("Opened https://example.com/org/repo/pull/7.")),
        ])

        # When
        runs = self.scan()["runs"]

        # Then
        self.assertEqual([(r["trigger"], r["outcome"]) for r in runs], [("invocation", "NO REPORT"), ("continue", "PR OPENED")])


class ReplayRunTest(ScanTestCase):
    def test_wait_for_a_coordinator_nudge_is_a_premature_return(self):
        # Given
        replay_returning_early(self.root)

        # When
        run = self.only_run()

        # Then
        self.assertEqual((run["kind"], run["case"]), (observe.SUBAGENT_KIND, "add-discount-rule"))
        self.assertEqual((run["by_category"]["premature return"], run["delivered"]), (995, True))


class SessionStoreTest(ScanTestCase):
    def test_scan_stores_each_session_as_a_session_line_then_one_line_per_run(self):
        # Given
        interactive_session(self.root)

        # When
        self.scan()

        # Then
        header, run = self.stored("session-1")
        self.assertEqual((header["session"]["runs"], header["session"]["extractor"]), (1, observe.EXTRACTOR_VERSION))
        self.assertEqual(run["run"]["outcome"], "PR OPENED")

    def test_session_mentioning_the_skill_without_a_run_is_stored_with_zero_runs(self):
        # Given
        session_mentioning_the_skill_without_running_it(self.root)

        # When
        self.scan()

        # Then
        self.assertEqual([line["session"]["runs"] for line in self.stored("session-3")], [0])

    def test_unchanged_session_is_not_extracted_again(self):
        # Given
        interactive_session(self.root)
        self.scan()
        extraction = self.spy_on_extraction()

        # When
        runs = self.scan()["runs"]

        # Then
        self.assertEqual((extraction.call_count, len(runs)), (0, 1))

    def test_session_whose_subagent_transcript_changed_is_extracted_again(self):
        # Given
        interactive_session(self.root)
        self.scan()
        with open(os.path.join(self.root, "project", "session-1", "subagents", "agent-" + CHILD_ID + ".jsonl"), "a") as handle:
            handle.write(json.dumps(assistant(81, "c3", text("STATUS: DONE, checked"))) + "\n")
        extraction = self.spy_on_extraction()

        # When
        self.scan()

        # Then
        self.assertEqual(extraction.call_count, 1)

    def test_session_extracted_by_another_extractor_version_is_extracted_again(self):
        # Given
        interactive_session(self.root)
        self.scan()
        header, run = self.stored("session-1")
        header["session"]["extractor"] = observe.EXTRACTOR_VERSION - 1
        write_jsonl(os.path.join(self.state, "sessions", "session-1.jsonl"), [header, run])
        extraction = self.spy_on_extraction()

        # When
        self.scan()

        # Then
        self.assertEqual(extraction.call_count, 1)

    def test_runs_of_a_pruned_session_are_kept_as_archived(self):
        # Given
        transcript = interactive_session(self.root)
        self.scan()
        os.remove(transcript)
        shutil.rmtree(transcript[:-len(".jsonl")])

        # When
        run = self.only_run()

        # Then
        self.assertEqual((run["archived"], run["outcome"], run["duration"]), (True, "PR OPENED", 160))

    def test_run_kept_from_an_older_extractor_leaves_unknown_fields_empty(self):
        # Given
        stored_session_of_an_older_extractor(self.state)

        # When
        run = self.only_run()

        # Then
        self.assertEqual((run["archived"], run["delivered"], run["llm_turns"]), (True, True, None))


class CommandClassificationTest(unittest.TestCase):
    def setUp(self):
        observe.use_profile(DELIVERY)

    def test_git_commit_behind_environment_variables_runs_the_pre_commit_suite(self):
        # When
        cause = cause_of('DOTNET_ROOT=/x PATH=/y:$PATH git -C a commit -m "feat: y"')

        # Then
        self.assertEqual(cause, (observe.GIT_COMMIT, False))

    def test_profile_command_wins_over_the_generic_rules(self):
        # When
        cause = cause_of("git add -A && hookrunner run pre-commit > log 2>&1")

        # Then
        self.assertEqual(cause, ("hook run by hand", False))

    def test_text_inside_a_heredoc_never_classifies_the_command(self):
        # When
        cause = cause_of("git push -u origin HEAD && cat > b.md <<'EOF'\nrun git commit then dotnet test\nEOF\ngh pr create")

        # Then
        self.assertEqual(cause, ("GitHub CLI", False))

    def test_text_inside_quotes_never_classifies_the_command(self):
        # When
        cause = cause_of('python3 learnings.py record --fix-why "run git commit twice"')

        # Then
        self.assertEqual(cause, ("shell read/edit", False))

    def test_text_inside_a_comment_never_classifies_the_command(self):
        # When
        cause = cause_of("sed -n 1,20p notes.md # sleep")

        # Then
        self.assertEqual(cause, ("shell read/edit", False))

    def test_sleep_inside_a_quoted_python_one_liner_is_polling(self):
        # When
        cause = cause_of('python3 -c "import time;time.sleep(25)"; echo waited')

        # Then
        self.assertEqual(cause, ("sleep / polling", True))

    def test_polling_loop_waits_on_the_subagents_running_meanwhile(self):
        # When
        cause = cause_of("until grep -q DONE f; do sleep 5; done")

        # Then
        self.assertEqual(cause, ("sleep / polling", True))


class HumanToolTest(unittest.TestCase):
    def test_question_asked_to_the_user_is_human_wait(self):
        # When
        cause = observe.classify_tool("AskUserQuestion", {"questions": []})

        # Then
        self.assertEqual(cause, ("human wait", False))


class OutcomeTest(unittest.TestCase):
    def setUp(self):
        observe.use_profile(DELIVERY)

    def test_aborted_outcome_mentioning_no_pr_opened_stays_aborted(self):
        # When
        label = observe.outcome_label("ABORTED (Phase 1) — no PR opened")

        # Then
        self.assertEqual(label, "ABORTED")

    def test_spec_implemented_outcome_mentioning_its_draft_pr_stays_spec_implemented(self):
        # When
        label = observe.outcome_label("SPEC IMPLEMENTED (draft PR opened, not merged)")

        # Then
        self.assertEqual(label, "SPEC IMPLEMENTED")

    def test_pull_request_opened_is_pr_opened(self):
        # When
        label = observe.outcome_label("pull request opened on branch workflow/x-1")

        # Then
        self.assertEqual(label, "PR OPENED")

    def test_lowercase_report_heading_is_a_workflow_report(self):
        # When
        found = observe.PROFILE.report.search("# Delivery report: tidy-imports, increment 1 of 2")

        # Then
        self.assertIsNotNone(found)

    def test_outcome_with_the_colon_inside_the_bold_is_read_with_its_increment(self):
        # Given
        report = "# Delivery report: tidy-imports, increment 1 of 2\n\n- **Outcome:** SUCCESS. Done."

        # When
        fields = observe.report_fields(report, report, created_pr=False)

        # Then
        self.assertEqual((fields["outcome"], fields["increment"], fields["increments_total"]), ("SUCCESS", 1, 2))

    def test_final_message_linking_a_pull_request_is_an_inferred_pr_opened(self):
        # When
        fields = observe.report_fields("", "I opened [org/repo#3593](https://example.com/org/repo/pull/3593).", created_pr=True)

        # Then
        self.assertEqual((fields["outcome"], fields["outcome_inferred"]), ("PR OPENED", True))

    def test_final_message_quoting_a_pull_request_the_run_did_not_create_is_no_report(self):
        # When
        fields = observe.report_fields("", "Stopped: the branch of https://example.com/org/repo/pull/3593 is busy.", created_pr=False)

        # Then
        self.assertEqual(fields["outcome"], "NO REPORT")

    def test_final_message_without_report_or_pull_request_is_no_report(self):
        # When
        fields = observe.report_fields("", "The refactor subagent is running in the background.", created_pr=False)

        # Then
        self.assertEqual(fields["outcome"], "NO REPORT")


class UserMessageTest(unittest.TestCase):
    def test_meta_message_from_the_coordinator_still_counts_as_its_nudge(self):
        # When
        kind = observe.classify_user_text("Resume.", True, "coordinator")

        # Then
        self.assertEqual(kind, "coordinator")

    def test_platform_retry_after_a_cut_off_stream_is_a_stall(self):
        # When
        kind = observe.classify_user_text("Your response above was cut off mid-stream.", True, None)

        # Then
        self.assertEqual(kind, "stall")

    def test_system_reminder_alone_is_not_a_human_prompt(self):
        # When
        kind = observe.classify_user_text("<system-reminder>worktree</system-reminder>", False, "human")

        # Then
        self.assertEqual(kind, "neutral")

    def test_typed_continue_is_a_human_prompt(self):
        # When
        kind = observe.classify_user_text("continue", False, "human")

        # Then
        self.assertEqual(kind, "human")


if __name__ == "__main__":
    unittest.main()
