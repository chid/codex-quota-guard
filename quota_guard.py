#!/usr/bin/env python3
"""Check CodexBar's widget cache or gate a Codex tool-use hook."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shlex
import sys
import tempfile

STOP_AT = 3
MAX_AGE = 600
OVERRIDE = "Ignore quota cutoff for this task"
OVERRIDE_LINE = re.compile(
    r"(?:ignore|override) (?:the )?(?:\d+(?:\.\d+)?% )?quota cutoff for this (?:task|turn)[.!]?",
    re.IGNORECASE,
)
WINDOW_IDS = {"session": "primary", "primary": "primary", "weekly": "secondary", "secondary": "secondary"}


def stamp(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_date(value):
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result.astimezone(timezone.utc) if result.tzinfo else None
    except (ValueError, OverflowError):
        return None


def number(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def read_json(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as f:
        temporary = Path(f.name)
        try:
            json.dump(value, f, allow_nan=False)
            f.flush()
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def check_snapshot(snapshot=None, stop_at=STOP_AT, max_age=MAX_AGE, now=None):
    """Read the widget file once. No subprocesses or network requests."""
    now = now or datetime.now(timezone.utc)
    report = {"provider": "codex", "source": "widget-snapshot", "checked_at": stamp(now),
              "status": "unavailable", "decision": "unknown", "stop_at_percent": stop_at, "windows": []}

    def unknown(reason):
        report["reason"] = reason
        return report

    paths = [snapshot] if snapshot is not None else list(
        (Path.home() / "Library/Group Containers").glob("*com.steipete.codexbar/widget-snapshot.json")
    )
    if len(paths) != 1:
        return unknown("widget_snapshot_missing_or_ambiguous")
    data = read_json(paths[0])
    if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
        return unknown("widget_snapshot_missing_or_invalid")
    entries = [e for e in data["entries"] if isinstance(e, dict) and e.get("provider") == "codex"]
    if len(entries) != 1:
        return unknown("widget_codex_entry_missing_or_ambiguous")
    entry = entries[0]
    updated = parse_date(entry.get("updatedAt"))
    if updated is None:
        return unknown("usage_timestamp_missing_or_invalid")
    age = (now-updated).total_seconds()
    report.update(updated_at=stamp(updated), age_seconds=round(age, 1))
    if age > max_age or age < -60:
        return unknown("stale_usage_or_clock_mismatch")
    windows = {k: entry.get(k) for k in ("primary", "secondary")}
    rows = entry.get("usageRows", [])
    if not isinstance(rows, list):
        return unknown("widget_usage_rows_invalid")
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str):
            return unknown("widget_usage_row_invalid")
        name = WINDOW_IDS.get(row["id"])
        if name is None:
            continue
        if name in seen:
            return unknown("widget_quota_window_duplicated")
        seen.add(name)
        window = row.get("window") if row.get("window") is not None else windows[name]
        left = row.get("percentLeft")
        if left is not None:
            if not number(left) or not 0 <= left <= 100:
                return unknown("widget_percent_left_invalid")
            if isinstance(window, dict) and number(window.get("usedPercent")):
                if abs(left - (100-window["usedPercent"])) > 0.001:
                    return unknown("widget_quota_values_inconsistent")
        windows[name] = window
    for name, raw in windows.items():
        window = {"name": name, "status": "unavailable"}
        report["windows"].append(window)
        if not isinstance(raw, dict):
            window["reason"] = "window_missing"
            continue
        used, duration = raw.get("usedPercent"), raw.get("windowMinutes")
        reset = parse_date(raw.get("resetsAt"))
        if not number(used) or not 0 <= used <= 100 or not number(duration) or duration <= 0 or reset is None:
            window["reason"] = "invalid_window_fields"
            continue
        if reset <= now:
            window["reason"] = "window_expired"
            continue
        window.update(status="ok", used_percent=used, remaining_percent=100-used,
                      window_minutes=duration, resets_at=stamp(reset))
    valid = [w for w in report["windows"] if w["status"] == "ok"]
    if not valid:
        return unknown("no_usable_windows")
    limiting = min(valid, key=lambda w: w["remaining_percent"])
    report.update(status="ok" if len(valid) == 2 else "partial",
                  limiting_reported_window=limiting["name"],
                  minimum_remaining_percent=limiting["remaining_percent"])
    if limiting["remaining_percent"] <= stop_at:
        report["decision"] = "stop"
    elif len(valid) == 2:
        report["decision"] = "above_threshold"
    else:
        report["reason"] = "incomplete_window_coverage"
    return report


def explicit_override(prompt):
    fence = None
    for line in prompt.splitlines():
        stripped = line.strip()
        if stripped.startswith(("```", "~~~")):
            marker = stripped[:3]
            if fence is None:
                fence = marker
            elif marker == fence:
                fence = None
        elif fence is None and OVERRIDE_LINE.fullmatch(stripped):
            return True
    return False


def denial(reason):
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": reason}}


class Guard:
    def __init__(self, root=None, snapshot=None, stop_at=STOP_AT, max_age=MAX_AGE):
        self.root = root or Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "codex-quota-guard"
        self.snapshot, self.stop_at, self.max_age = snapshot, stop_at, max_age
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)

    def state_path(self, event):
        session, turn = event.get("session_id"), event.get("turn_id")
        if not isinstance(session, str) or not session or not isinstance(turn, str) or not turn:
            raise ValueError("missing_session_or_turn")
        key = hashlib.sha256((session + "\0" + turn).encode()).hexdigest()
        return self.root / "turns" / (key + ".json")

    def fetch(self):
        return check_snapshot(self.snapshot, self.stop_at, self.max_age)

    def audit(self, event, decision, report=None):
        key = hashlib.sha256(event["session_id"].encode()).hexdigest()
        write_json(self.root / ("last-check-" + key + ".json"), {
            "session_id": event["session_id"], "turn_id": event["turn_id"],
            "tool_use_id": event.get("tool_use_id"), "tool_name": event.get("tool_name"),
            "checked_at": stamp(datetime.now(timezone.utc)), "decision": decision, "source": "widget-snapshot",
            "minimum_remaining_percent": (report or {}).get("minimum_remaining_percent"),
            "quota_updated_at": (report or {}).get("updated_at"),
        })

    def block_reason(self, report):
        if report.get("decision") == "stop":
            windows = [w for w in report["windows"]
                       if w.get("status") == "ok" and w["remaining_percent"] <= self.stop_at]
            detail = "; ".join(
                f"{'session' if w['name'] == 'primary' else 'weekly'} {w['remaining_percent']:g}% remaining, resets {w['resets_at']}"
                for w in windows
            )
            reason = f"Codex quota cutoff reached: {detail}. The cutoff is {self.stop_at:g}%."
        else:
            reason = "Codex widget quota could not be verified: " + str(report.get("reason", "unknown")) + ". Refresh CodexBar if its snapshot is unavailable or stale."
        return reason + f" Stop tool use and end the turn with a brief progress summary. Do not retry or bypass the guard. The user can override for one turn by adding a standalone line: {OVERRIDE}."

    def handle(self, event):
        kind = event.get("hook_event_name")
        path = self.state_path(event)
        state = read_json(path)
        if state is None:
            state = {}
        if not isinstance(state, dict):
            raise ValueError("invalid_turn_state")
        if kind == "UserPromptSubmit":
            override = explicit_override(event.get("prompt", ""))
            write_json(path, {"override": override})
            if override:
                return {"hookSpecificOutput": {"hookEventName": kind,
                        "additionalContext": "The user explicitly overrode the quota cutoff for this turn only."}}
            return None
        if kind == "Stop":
            return {"continue": False, "stopReason": state["blocked"]} if state.get("blocked") else {}
        if kind != "PreToolUse":
            raise ValueError("unsupported_hook_event")
        if state.get("override") is True:
            self.audit(event, "override")
            return None
        if state.get("blocked"):
            self.audit(event, "blocked")
            return denial(state["blocked"])
        try:
            report = self.fetch()
        except (OSError, ValueError, TypeError) as error:
            report = {"decision": "unknown", "reason": "widget_check_" + type(error).__name__}
        self.audit(event, report["decision"], report)
        if report["decision"] == "above_threshold":
            return None
        reason = self.block_reason(report)
        write_json(path, {"override": False, "blocked": reason})
        return denial(reason)


def hook_config(command):
    return {"hooks": {event: [{"hooks": [{"type": "command", "command": command, "timeout": 3}]}]
                      for event in ("PreToolUse", "UserPromptSubmit", "Stop")}}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="Print quota JSON. Exit 0=above cutoff, 3=cutoff reached, 2=unknown.")
    mode.add_argument("--print-hooks", action="store_true", help="Print hook configuration for this script and its options.")
    parser.add_argument("--snapshot", type=Path, help="Use a specific widget-snapshot.json instead of discovering it.")
    parser.add_argument("--stop-at", type=float, default=STOP_AT, help="Inclusive remaining-percent cutoff, default 3.")
    parser.add_argument("--max-age", type=float, default=MAX_AGE, help="Maximum provider reading age in seconds, default 600.")
    parser.add_argument("--state-dir", type=Path, help="Override the directory for private per-turn state.")
    args = parser.parse_args(argv)
    if not number(args.stop_at) or not 0 <= args.stop_at <= 100:
        parser.error("--stop-at must be between 0 and 100")
    if not number(args.max_age) or args.max_age <= 0:
        parser.error("--max-age must be positive and finite")
    if args.print_hooks:
        command = [sys.executable, str(Path(__file__).resolve())]
        for flag, value in (("--snapshot", args.snapshot), ("--stop-at", args.stop_at),
                            ("--max-age", args.max_age), ("--state-dir", args.state_dir)):
            if value is not None:
                command.extend([flag, str(value)])
        print(json.dumps(hook_config(shlex.join(command)), indent=2))
        return 0
    if args.check:
        report = check_snapshot(args.snapshot, args.stop_at, args.max_age)
        print(json.dumps(report, indent=2, allow_nan=False))
        return {"above_threshold": 0, "stop": 3, "unknown": 2}[report["decision"]]
    event = {}
    try:
        event = json.load(sys.stdin)
        if not isinstance(event, dict):
            event = {}
            raise ValueError("invalid_hook_input")
        output = Guard(args.state_dir, args.snapshot, args.stop_at, args.max_age).handle(event)
    except Exception as error:
        reason = f"Codex quota guard could not verify permission ({type(error).__name__}). Stop tool use and report the failure."
        output = {"continue": False, "stopReason": reason} if event.get("hook_event_name") == "Stop" else denial(reason)
    if output is not None:
        print(json.dumps(output))
    return 0


if __name__ == "__main__":
    sys.exit(main())
