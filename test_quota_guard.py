from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from quota_guard import Guard, check_snapshot, explicit_override, stamp, write_json


class QuotaGuardTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        base = Path(self.directory.name)
        self.snapshot = base / "widget-snapshot.json"
        self.guard = Guard(root=base / "guard", snapshot=self.snapshot)
        self.event = {"session_id": "session-one", "turn_id": "turn-one",
                      "hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_use_id": "call-one"}

    def snapshot_data(self, session=50, weekly=50, age=0):
        now = datetime.now(timezone.utc)
        windows = [("session", session, 300, timedelta(hours=1)),
                   ("weekly", weekly, 10080, timedelta(days=1))]
        return {"generatedAt": stamp(now), "entries": [{
            "provider": "codex", "updatedAt": stamp(now-timedelta(seconds=age)),
            "usageRows": [{"id": name, "percentLeft": left, "window": {
                "usedPercent": 100-left, "windowMinutes": minutes,
                "resetsAt": stamp(now+reset)}} for name,left,minutes,reset in windows],
        }]}

    def call(self, **values):
        write_json(self.snapshot, self.snapshot_data(**values))
        return self.guard.handle(self.event)

    def test_above_cutoff_allows_without_cli(self):
        with patch("subprocess.run", side_effect=AssertionError("must not launch CLI")):
            self.assertIsNone(self.call(session=3.1))

    def test_session_at_three_is_denied(self):
        output = self.call(session=3)
        self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("session 3% remaining", output["hookSpecificOutput"]["permissionDecisionReason"])

    def test_weekly_below_three_is_denied_for_mcp(self):
        self.event["tool_name"] = "mcp__fs__read"
        output = self.call(weekly=2)
        self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("weekly 2% remaining", output["hookSpecificOutput"]["permissionDecisionReason"])

    def test_five_minute_cache_is_accepted(self):
        self.assertIsNone(self.call(age=310))

    def test_old_entry_is_denied_despite_recent_snapshot_generation(self):
        output = self.call(age=601)
        self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("stale_usage", output["hookSpecificOutput"]["permissionDecisionReason"])

    def test_missing_snapshot_denies_and_prevents_stop_continuation(self):
        output = self.guard.handle(self.event)
        self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIs(self.guard.handle(dict(self.event, hook_event_name="Stop"))["continue"], False)

    def test_denial_stays_blocked_for_the_turn(self):
        self.call(session=2)
        with patch.object(self.guard, "fetch", side_effect=AssertionError("must remain stopped")):
            output = self.guard.handle(self.event)
        self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_read_failure_prevents_stop_continuation(self):
        with patch.object(self.guard, "fetch", side_effect=OSError()):
            output = self.guard.handle(self.event)
        self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIs(self.guard.handle(dict(self.event, hook_event_name="Stop"))["continue"], False)

    def test_explicit_override_does_not_read_cache_or_leak(self):
        self.guard.handle(dict(self.event, hook_event_name="UserPromptSubmit",
                               prompt="Ignore quota cutoff for this task.\nRun my command."))
        with patch.object(self.guard, "fetch", side_effect=AssertionError("must not read quota")):
            self.assertIsNone(self.guard.handle(self.event))
        for changes in ({"turn_id": "turn-two"}, {"session_id": "session-two"}):
            output = self.guard.handle(dict(self.event, **changes))
            self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_continue_and_quoted_examples_do_not_override(self):
        for prompt in ("continue", 'Say "Ignore quota cutoff for this task"',
                       "```\nIgnore quota cutoff for this task\n```", "> Ignore quota cutoff for this task",
                       "```\n~~~\nIgnore quota cutoff for this task\n```"):
            self.assertFalse(explicit_override(prompt))
        self.assertTrue(explicit_override("Ignore the 3% quota cutoff for this turn!"))

    def test_each_call_reads_the_latest_widget_file(self):
        self.assertIsNone(self.call())
        output = self.call(weekly=1)
        self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_expired_window_cannot_allow_work(self):
        data = self.snapshot_data()
        data["entries"][0]["usageRows"][0]["window"]["resetsAt"] = "2000-01-01T00:00:00Z"
        write_json(self.snapshot, data)
        self.assertEqual(self.guard.fetch()["decision"], "unknown")

    def test_corrupt_cache_does_not_launch_cli(self):
        self.snapshot.write_text("broken json")
        with patch("subprocess.run", side_effect=AssertionError("must not launch CLI")):
            output = self.guard.handle(self.event)
        self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_ambiguous_codex_entries_are_denied(self):
        data = self.snapshot_data()
        data["entries"].append(data["entries"][0].copy())
        write_json(self.snapshot, data)
        self.assertEqual(self.guard.fetch()["decision"], "unknown")

    def test_multiple_app_group_files_are_ambiguous(self):
        with patch("pathlib.Path.glob", return_value=[self.snapshot, self.snapshot]):
            self.assertEqual(check_snapshot()["decision"], "unknown")

    def test_missing_default_app_group_is_unknown(self):
        with patch("pathlib.Path.glob", return_value=[]):
            self.assertEqual(check_snapshot()["decision"], "unknown")

    def test_display_percentage_and_window_cannot_disagree(self):
        data = self.snapshot_data()
        data["entries"][0]["usageRows"][0]["percentLeft"] = 1
        write_json(self.snapshot, data)
        self.assertEqual(self.guard.fetch()["decision"], "unknown")

    def test_invalid_percentages_are_denied(self):
        for value in (True, -1, 101, "99", float("nan"), 10**1000):
            data = self.snapshot_data()
            data["entries"][0]["usageRows"][0]["percentLeft"] = value
            self.snapshot.write_text(json.dumps(data))
            self.assertEqual(self.guard.fetch()["decision"], "unknown")

    def test_duplicate_session_rows_are_unknown(self):
        data = self.snapshot_data()
        data["entries"][0]["usageRows"].append(data["entries"][0]["usageRows"][0].copy())
        write_json(self.snapshot, data)
        self.assertEqual(self.guard.fetch()["decision"], "unknown")

    def test_native_windows_are_usable_without_display_rows(self):
        data = self.snapshot_data()
        entry = data["entries"][0]
        entry["primary"], entry["secondary"] = [row["window"] for row in entry.pop("usageRows")]
        write_json(self.snapshot, data)
        self.assertEqual(self.guard.fetch()["decision"], "above_threshold")

    def test_report_omits_account_identity_and_token_history(self):
        data = self.snapshot_data()
        data["entries"][0].update(quotaOwnerKey="private-account", tokenUsage={"sessionTokens": 12345})
        write_json(self.snapshot, data)
        report = json.dumps(self.guard.fetch())
        self.assertNotIn("private-account", report)
        self.assertNotIn("sessionTokens", report)

    def test_audit_contains_no_prompt_or_tool_input(self):
        self.event["tool_input"] = {"command": "a-secret-value"}
        self.call()
        for file in self.guard.root.rglob("*.json"):
            self.assertNotIn("a-secret-value", file.read_text())
            self.assertEqual(file.stat().st_mode & 0o777, 0o600)

    def cli(self, *args, input=None):
        return subprocess.run([sys.executable, str(Path(__file__).with_name("quota_guard.py")),
                               "--snapshot", str(self.snapshot), *args],
                              input=input, capture_output=True, text=True, timeout=5)

    def test_cli_check_exit_codes_and_literal_percentages(self):
        for left, expected in ((4, 0), (3, 3), (2, 3)):
            write_json(self.snapshot, self.snapshot_data(session=left))
            result = self.cli("--check")
            self.assertEqual(result.returncode, expected, result.stderr)
            self.assertEqual(json.loads(result.stdout)["windows"][0]["remaining_percent"], left)
        self.snapshot.unlink()
        self.assertEqual(self.cli("--check").returncode, 2)

    def test_cli_custom_threshold_and_age(self):
        write_json(self.snapshot, self.snapshot_data(session=4, age=310))
        self.assertEqual(self.cli("--check", "--stop-at", "5").returncode, 3)
        self.assertEqual(self.cli("--check", "--max-age", "300").returncode, 2)

    def test_cli_hook_consumes_stdin_and_denies(self):
        write_json(self.snapshot, self.snapshot_data(session=3))
        result = self.cli("--state-dir", str(self.guard.root), input=json.dumps(self.event))
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_cli_malformed_hook_input_denies(self):
        for text in ("not-json", "[]"):
            result = self.cli("--state-dir", str(self.guard.root), input=text)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_cli_generated_hooks_preserve_options_and_shell_quoting(self):
        result = self.cli("--print-hooks", "--stop-at", "5", "--state-dir", "/tmp/state with spaces")
        self.assertEqual(result.returncode, 0)
        hooks = json.loads(result.stdout)["hooks"]
        self.assertEqual(set(hooks), {"PreToolUse", "UserPromptSubmit", "Stop"})
        for groups in hooks.values():
            command = shlex.split(groups[0]["hooks"][0]["command"])
            self.assertIn("/tmp/state with spaces", command)
            self.assertIn("5.0", command)

    def test_cli_rejects_invalid_limits(self):
        for args in (("--stop-at", "nan"), ("--stop-at", "101"), ("--max-age", "0")):
            self.assertEqual(self.cli("--check", *args).returncode, 2)


if __name__ == "__main__":
    unittest.main()
