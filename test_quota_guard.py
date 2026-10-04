from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from quota_guard import (Guard, brief, check_snapshot, doctor, explicit_override,
                         install, load_hook_inventory, main, stamp, write_json)


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
        with patch("quota_guard.subprocess.Popen", side_effect=AssertionError("must not launch CLI")):
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
                               prompt="Ignore quota cutoff for this task\nRun my command."))
        with patch.object(self.guard, "fetch", side_effect=AssertionError("must not read quota")):
            self.assertIsNone(self.guard.handle(self.event))
        for changes in ({"turn_id": "turn-two"}, {"session_id": "session-two"}):
            output = self.guard.handle(dict(self.event, **changes))
            self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_continue_and_quoted_examples_do_not_override(self):
        for prompt in ("continue", 'Say "Ignore quota cutoff for this task"',
                       "```\nIgnore quota cutoff for this task\n```", "> Ignore quota cutoff for this task",
                       "```\n~~~\nIgnore quota cutoff for this task\n```",
                       "Ignore the 3% quota cutoff for this turn!", "Ignore quota cutoff for this task.",
                       "ignore quota cutoff for this task"):
            self.assertFalse(explicit_override(prompt))
        self.assertTrue(explicit_override("Run a check.\nIgnore quota cutoff for this task\nThen continue."))

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
        with patch("quota_guard.subprocess.Popen", side_effect=AssertionError("must not launch CLI")):
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

    def test_cli_independent_cutoffs_use_each_window_inclusively(self):
        for session, weekly, expected in ((5, 90, 3), (90, 10, 3), (5.1, 10.1, 0)):
            with self.subTest(session=session, weekly=weekly):
                write_json(self.snapshot, self.snapshot_data(session=session, weekly=weekly))
                result = self.cli("--check", "--session-stop-at", "5", "--weekly-stop-at", "10")
                self.assertEqual(result.returncode, expected, result.stderr)
                report = json.loads(result.stdout)
                self.assertEqual(report["cutoffs_percent"], {"primary": 5, "secondary": 10})
                self.assertEqual([w["stop_at_percent"] for w in report["windows"]], [5, 10])

    def test_weekly_cutoff_triggers_even_when_session_has_less_remaining(self):
        write_json(self.snapshot, self.snapshot_data(session=4, weekly=6))
        result = self.cli("--check", "--weekly-stop-at", "10")
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertEqual(json.loads(result.stdout)["cutoffs_percent"], {"primary": 3, "secondary": 10})

    def test_specific_cutoffs_override_shared_cutoff_in_either_option_order(self):
        write_json(self.snapshot, self.snapshot_data(session=2, weekly=3))
        for options in (("--stop-at", "5", "--session-stop-at", "1", "--weekly-stop-at", "2"),
                        ("--weekly-stop-at", "2", "--session-stop-at", "1", "--stop-at", "5")):
            result = self.cli("--check", *options)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["cutoffs_percent"], {"primary": 1, "secondary": 2})

    def test_unset_window_inherits_shared_cutoff(self):
        write_json(self.snapshot, self.snapshot_data(session=6, weekly=11))
        result = self.cli("--check", "--stop-at", "5", "--weekly-stop-at", "10")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["cutoffs_percent"], {"primary": 5, "secondary": 10})

    def test_independent_cutoffs_keep_missing_or_stale_readings_blocked(self):
        for age, remove_weekly, expected in ((601, False, "unknown"), (0, True, "unknown")):
            data = self.snapshot_data(session=50, age=age)
            if remove_weekly:
                data["entries"][0]["usageRows"].pop()
            write_json(self.snapshot, data)
            report = check_snapshot(self.snapshot, session_stop_at=5, weekly_stop_at=10)
            self.assertEqual(report["decision"], expected)

    def test_cli_hook_denial_reports_the_window_specific_cutoff(self):
        write_json(self.snapshot, self.snapshot_data(session=50, weekly=8))
        result = self.cli("--state-dir", str(self.guard.root), "--session-stop-at", "5",
                          "--weekly-stop-at", "10", input=json.dumps(self.event))
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)["hookSpecificOutput"]
        self.assertEqual(output["permissionDecision"], "deny")
        self.assertIn("weekly 8% remaining, cutoff 10%, resets", output["permissionDecisionReason"])
        self.assertNotIn("session 50% remaining", output["permissionDecisionReason"])

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
            self.assertIn(str(Path("/tmp/state with spaces").resolve()), command)
            self.assertIn("5.0", command)

    def test_cli_rejects_invalid_limits(self):
        for flag in ("--stop-at", "--session-stop-at", "--weekly-stop-at"):
            for value in ("nan", "inf", "-1", "101"):
                self.assertEqual(self.cli("--check", flag, value).returncode, 2)
        self.assertEqual(self.cli("--check", "--max-age", "0").returncode, 2)

    def test_brief_literal_output_and_age(self):
        data = self.snapshot_data(session=88, weekly=94, age=310)
        write_json(self.snapshot, data)
        now = datetime.fromisoformat(data["generatedAt"].replace("Z", "+00:00"))
        self.assertEqual(brief(check_snapshot(self.snapshot, now=now)),
                         "Session 88% | Weekly 94% | Updated 5m ago")

    def test_cli_brief_exit_codes_and_cutoff_message(self):
        write_json(self.snapshot, self.snapshot_data(session=3))
        result = self.cli("--brief")
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn("Session 3% | Weekly 50%", result.stdout)
        self.assertIn("STOP (3% cutoff)", result.stdout)
        self.assertEqual(len(result.stdout.splitlines()), 1)
        write_json(self.snapshot, self.snapshot_data(session=4))
        self.assertEqual(self.cli("--brief").returncode, 0)
        self.assertEqual(self.cli("--brief", "--stop-at", "5").returncode, 3)
        self.snapshot.unlink()
        result = self.cli("--brief")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout.strip(),
                         "Session ? | Weekly ? | UNAVAILABLE (widget_snapshot_missing_or_invalid)")

    def test_cli_brief_reports_effective_independent_cutoffs(self):
        write_json(self.snapshot, self.snapshot_data(session=50, weekly=8))
        result = self.cli("--brief", "--session-stop-at", "5", "--weekly-stop-at", "10")
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn("STOP (Session 5% / Weekly 10% cutoffs)", result.stdout)

    def test_brief_unknown_remains_unavailable_with_partial_quota(self):
        data = self.snapshot_data()
        data["entries"][0]["usageRows"].pop()
        write_json(self.snapshot, data)
        report = check_snapshot(self.snapshot)
        self.assertEqual(report["decision"], "unknown")
        self.assertIn("Session 50% | Weekly ?", brief(report))
        self.assertIn("UNAVAILABLE (incomplete_window_coverage)", brief(report))

    def doctor_fixture(self):
        home = Path(self.directory.name) / "codex"
        target = home / "hooks/quota_guard.py"
        target.parent.mkdir(parents=True)
        target.write_bytes(Path(__file__).with_name("quota_guard.py").read_bytes())
        inventory = {"data": [{"errors": [], "hooks": [
            {"eventName": event, "command": shlex.join([sys.executable, str(target)]),
             "enabled": True, "trustStatus": "trusted", "matcher": None, "async": False}
            for event in ("preToolUse", "userPromptSubmit", "stop")]}]}
        write_json(self.snapshot, self.snapshot_data())
        return home, inventory

    def test_doctor_checks_native_enablement_and_trust(self):
        home, inventory = self.doctor_fixture()
        with patch("quota_guard.load_hook_inventory", return_value=inventory):
            checks = doctor(home, self.snapshot)
        self.assertEqual([name for ok, name, _ in checks if ok],
                         ["Widget snapshot", "Installed script", "Hook configuration",
                          "preToolUse", "userPromptSubmit", "stop"])
        self.assertTrue(all(ok for ok, _, _ in checks))

    def test_doctor_reports_disabled_modified_or_missing_hooks(self):
        home, inventory = self.doctor_fixture()
        hooks = inventory["data"][0]["hooks"]
        hooks[0]["enabled"] = False
        hooks[1]["trustStatus"] = "modified"
        hooks.pop()
        with patch("quota_guard.load_hook_inventory", return_value=inventory):
            checks = doctor(home, self.snapshot)
        self.assertEqual([(ok, name) for ok, name, _ in checks[-3:]],
                         [(False, "preToolUse"), (False, "userPromptSubmit"), (False, "stop")])
        self.assertIn("review and enable", checks[-2][2])
        self.assertIn("Not registered", checks[-1][2])

    def test_doctor_rejects_restricted_async_and_checker_hooks(self):
        home, inventory = self.doctor_fixture()
        hooks = inventory["data"][0]["hooks"]
        hooks[0]["matcher"] = "Bash"
        hooks[1]["async"] = True
        hooks[2]["command"] += " --check"
        with patch("quota_guard.load_hook_inventory", return_value=inventory):
            checks = doctor(home, self.snapshot)
        self.assertTrue(all(not ok for ok, _, _ in checks[-3:]))
        self.assertIn("Restricted matcher", checks[-3][2])
        self.assertIn("not in hook mode", checks[-1][2])

    def test_doctor_detects_old_script_stale_cache_and_config_errors(self):
        home, inventory = self.doctor_fixture()
        (home / "hooks/quota_guard.py").write_text("# old script\n")
        write_json(self.snapshot, self.snapshot_data(age=601))
        inventory["data"][0]["errors"] = [{"message": "Bad hooks.json"}]
        with patch("quota_guard.load_hook_inventory", return_value=inventory):
            checks = doctor(home, self.snapshot)
        self.assertEqual([(ok, name) for ok, name, _ in checks[:3]],
                         [(False, "Widget snapshot"), (False, "Installed script"),
                          (False, "Hook configuration")])

    def test_doctor_missing_cli_has_actionable_failure_and_exit_code(self):
        home, _ = self.doctor_fixture()
        output = io.StringIO()
        with patch("quota_guard.shutil.which", return_value=None), redirect_stdout(output):
            result = main(["--doctor", "--snapshot", str(self.snapshot), "--codex-home", str(home)])
        self.assertEqual(result, 2)
        self.assertIn("FAIL Hook inventory", output.getvalue())
        self.assertIn("check PATH and /hooks", output.getvalue())

    def test_doctor_valid_setup_reports_cutoff_without_invalidating_installation(self):
        home, inventory = self.doctor_fixture()
        write_json(self.snapshot, self.snapshot_data(session=3))
        output = io.StringIO()
        with patch("quota_guard.load_hook_inventory", return_value=inventory), redirect_stdout(output):
            result = main(["--doctor", "--snapshot", str(self.snapshot), "--codex-home", str(home)])
        self.assertEqual(result, 0)
        self.assertIn("STOP (3% cutoff)", output.getvalue())

    def test_doctor_uses_requested_independent_cutoffs(self):
        home, inventory = self.doctor_fixture()
        write_json(self.snapshot, self.snapshot_data(session=50, weekly=8))
        output = io.StringIO()
        with patch("quota_guard.load_hook_inventory", return_value=inventory), redirect_stdout(output):
            result = main(["--doctor", "--snapshot", str(self.snapshot), "--codex-home", str(home),
                           "--session-stop-at", "5", "--weekly-stop-at", "10"])
        self.assertEqual(result, 0)
        self.assertIn("STOP (Session 5% / Weekly 10% cutoffs)", output.getvalue())

    def test_native_inventory_protocol_queries_hooks_without_starting_a_task(self):
        executable = Path(self.directory.name) / "codex"
        executable.write_text("#!/usr/bin/env python3\n" + """
import json
import os
import sys
assert sys.argv[1:] == ['app-server', '--stdio']
methods = []
for line in sys.stdin:
    request = json.loads(line)
    if 'id' not in request:
        continue
    method = request['method']
    methods.append(method)
    assert method in ('initialize', 'hooks/list')
    result = {} if method == 'initialize' else {
        'data': [{'cwd': os.environ['CODEX_HOME'], 'errors': [], 'hooks': []}],
        'methods': methods}
    print(json.dumps({'id': request['id'], 'result': result}), flush=True)
""")
        executable.chmod(0o700)
        home = Path(self.directory.name) / "codex home"
        with patch("quota_guard.shutil.which", return_value=str(executable)):
            result = load_hook_inventory(home, Path(self.directory.name))
        self.assertEqual(result["methods"], ["initialize", "hooks/list"])
        self.assertEqual(result["data"], [{"cwd": str(home), "errors": [], "hooks": []}])


class InstallerTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.home = Path(directory.name) / "codex home"
        self.home.mkdir()
        self.target = self.home / "hooks/quota_guard.py"
        self.config = self.home / "hooks.json"
        self.source = Path(__file__).with_name("quota_guard.py")
        self.command = shlex.join([sys.executable, str(self.target), "--stop-at", "3", "--max-age", "600"])

    def test_fresh_install_and_quoted_command(self):
        result = install(self.home, self.command)
        self.assertTrue(result["changed"])
        self.assertTrue(result["review"])
        self.assertEqual(self.target.read_bytes(), self.source.read_bytes())
        hooks = json.loads(self.config.read_text())["hooks"]
        self.assertEqual(set(hooks), {"PreToolUse", "UserPromptSubmit", "Stop"})
        for groups in hooks.values():
            handler = groups[0]["hooks"][0]
            self.assertEqual(shlex.split(handler["command"])[1], str(self.target))
            self.assertIs(handler["async"], False)
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o600)

    def test_preserves_unrelated_hooks_and_other_config(self):
        unrelated = {"type": "command", "command": "notify-me", "timeout": 10}
        config = {"description": "My hooks", "hooks": {
            "PreToolUse": [{"matcher": "Bash", "hooks": [unrelated]}],
            "Stop": [{"hooks": [unrelated]}], "SessionStart": [{"hooks": [unrelated]}]}}
        self.config.write_text(json.dumps(config, indent=2))
        install(self.home, self.command)
        updated = json.loads(self.config.read_text())
        self.assertEqual(updated["description"], "My hooks")
        self.assertEqual(updated["hooks"]["SessionStart"], config["hooks"]["SessionStart"])
        for event in ("PreToolUse", "Stop"):
            self.assertEqual(updated["hooks"][event][0], config["hooks"][event][0])
            self.assertEqual(len(updated["hooks"][event]), 2)

    def test_repeat_install_does_not_write_or_make_backups(self):
        install(self.home, self.command)
        before = (self.config.read_bytes(), self.config.stat().st_mtime_ns, self.target.stat().st_mtime_ns,
                  list((self.home / "hooks/backups").iterdir()))
        result = install(self.home, self.command)
        after = (self.config.read_bytes(), self.config.stat().st_mtime_ns, self.target.stat().st_mtime_ns,
                 list((self.home / "hooks/backups").iterdir()))
        self.assertFalse(result["changed"])
        self.assertIsNone(result["backup"])
        self.assertEqual(before, after)

    def test_upgrade_backs_up_exact_originals_privately(self):
        old_config = b'{"hooks": {}}\n'
        old_script = b"# old script\n"
        self.config.write_bytes(old_config)
        self.target.parent.mkdir()
        self.target.write_bytes(old_script)
        result = install(self.home, self.command)
        self.assertEqual((result["backup"] / "hooks.json").read_bytes(), old_config)
        self.assertEqual((result["backup"] / "quota_guard.py").read_bytes(), old_script)
        self.assertEqual(result["backup"].stat().st_mode & 0o777, 0o700)
        for path in result["backup"].iterdir():
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_malformed_config_leaves_config_and_script_untouched(self):
        self.target.parent.mkdir()
        self.target.write_bytes(b"# original\n")
        for content in ("not json", "[]", '{"hooks": []}',
                        '{"hooks":{"PreToolUse":"bad"}}', '{"hooks":{"Stop":[{}]}}'):
            self.config.write_text(content)
            with self.assertRaises(ValueError):
                install(self.home, self.command)
            self.assertEqual(self.config.read_text(), content)
            self.assertEqual(self.target.read_bytes(), b"# original\n")
            self.assertFalse((self.home / "hooks/backups").exists())

    def test_updates_scoped_and_duplicate_guards_preserving_other_positions(self):
        guard = {"type": "command", "command": self.command, "async": True}
        unrelated = {"type": "command", "command": "notify-me"}
        other_guard = {"type": "command", "command": "python3 /different/quota_guard.py"}
        config = {"hooks": {"PreToolUse": [
            {"matcher": "Bash", "hooks": [guard, unrelated]},
            {"hooks": [dict(guard, statusMessage="Quota"), other_guard]},
            {"hooks": [guard]}]}}
        self.config.write_text(json.dumps(config))
        install(self.home, self.command)
        groups = json.loads(self.config.read_text())["hooks"]["PreToolUse"]
        self.assertEqual(groups[0]["matcher"], "Bash")
        self.assertEqual(groups[0]["hooks"][1], unrelated)
        self.assertIs(groups[0]["hooks"][0]["async"], False)
        self.assertEqual(groups[1]["hooks"][0]["statusMessage"], "Quota")
        self.assertIs(groups[1]["hooks"][0]["async"], False)
        self.assertEqual(groups[1]["hooks"][1], other_guard)
        self.assertEqual(len(groups), 3)
        self.assertEqual(len(groups[2]["hooks"]), 1)
        self.assertIs(groups[2]["hooks"][0]["async"], False)

    def test_only_scoped_guard_gets_an_all_tool_group_without_moving_handlers(self):
        unrelated = {"type": "command", "command": "notify-me"}
        config = {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
            {"type": "command", "command": self.command}, unrelated]}]}}
        self.config.write_text(json.dumps(config))
        install(self.home, self.command)
        groups = json.loads(self.config.read_text())["hooks"]["PreToolUse"]
        self.assertEqual(groups[0]["hooks"][1], unrelated)
        self.assertEqual(len(groups), 2)
        self.assertNotIn("matcher", groups[1])
        self.assertEqual(groups[1]["hooks"][0]["command"], self.command)

    def test_config_write_failure_restores_old_script(self):
        old_config = b'{"hooks": {}}'
        self.config.write_bytes(old_config)
        self.target.parent.mkdir()
        self.target.write_bytes(b"# original\n")
        with patch("quota_guard.write_json", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                install(self.home, self.command)
        self.assertEqual(self.config.read_bytes(), old_config)
        self.assertEqual(self.target.read_bytes(), b"# original\n")

    def test_config_write_failure_removes_new_script(self):
        self.config.write_text('{"hooks": {}}')
        with patch("quota_guard.write_json", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                install(self.home, self.command)
        self.assertEqual(self.config.read_text(), '{"hooks": {}}')
        self.assertFalse(self.target.exists())

    def test_cli_install_resolves_relative_option_paths(self):
        result = subprocess.run([sys.executable, str(self.source.resolve()), "--install",
                                 "--codex-home", str(self.home), "--snapshot", "cache with spaces.json",
                                 "--state-dir", "state with spaces", "--stop-at", "5"],
                                cwd=self.home, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        command = json.loads(self.config.read_text())["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        arguments = shlex.split(command)
        self.assertIn(str((self.home / "cache with spaces.json").resolve()), arguments)
        self.assertIn(str((self.home / "state with spaces").resolve()), arguments)
        self.assertIn("5.0", arguments)

    def test_cli_install_preserves_independent_cutoffs_in_all_hook_commands(self):
        result = subprocess.run([sys.executable, str(self.source.resolve()), "--install",
                                 "--codex-home", str(self.home), "--session-stop-at", "5",
                                 "--weekly-stop-at", "10"], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        hooks = json.loads(self.config.read_text())["hooks"]
        for groups in hooks.values():
            command = shlex.split(groups[0]["hooks"][0]["command"])
            self.assertEqual(command[command.index("--session-stop-at")+1], "5.0")
            self.assertEqual(command[command.index("--weekly-stop-at")+1], "10.0")


if __name__ == "__main__":
    unittest.main()
