#!/usr/bin/env python3
"""Check CodexBar's widget cache or gate a Codex tool-use hook."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import selectors
import shlex
import shutil
import subprocess
import sys
import tempfile
import time

STOP_AT = 3
MAX_AGE = 600
OVERRIDE = "Ignore quota cutoff for this task"
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
        elif fence is None and stripped == OVERRIDE:
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


def brief(report):
    labels = {"primary": "Session", "secondary": "Weekly"}
    windows = {w["name"]: w for w in report["windows"]}
    parts = []
    for name, label in labels.items():
        window = windows.get(name, {})
        value = f"{window['remaining_percent']:g}%" if window.get("status") == "ok" else "?"
        parts.append(label + " " + value)
    if "age_seconds" in report:
        age = max(0, int(report["age_seconds"]))
        elapsed = f"{age}s" if age < 60 else f"{age // 60}m" if age < 3600 else f"{age // 3600}h"
        parts.append("Updated " + elapsed + " ago")
    if report["decision"] == "stop":
        parts.append(f"STOP ({report['stop_at_percent']:g}% cutoff)")
    elif report["decision"] == "unknown":
        parts.append("UNAVAILABLE (" + report.get("reason", "unknown") + ")")
    return " | ".join(parts)


def command_for(script, args):
    command = [sys.executable, str(script)]
    for flag, value in (("--snapshot", args.snapshot), ("--stop-at", args.stop_at),
                        ("--max-age", args.max_age), ("--state-dir", args.state_dir)):
        if value is not None:
            if isinstance(value, Path):
                value = value.expanduser().resolve()
            command.extend([flag, str(value)])
    return shlex.join(command)


def guard_script(command):
    if not isinstance(command, str):
        return None
    try:
        parts = shlex.split(command)
    except ValueError:
        return None
    for part in parts[:2]:
        path = Path(part).expanduser()
        if path.name == "quota_guard.py":
            return path.resolve()
    return None


def install(codex_home, command, source=None):
    source = source or Path(__file__).resolve()
    target = codex_home / "hooks/quota_guard.py"
    config_path = codex_home / "hooks.json"
    original = config_path.read_bytes() if config_path.exists() else None
    config = json.loads(original) if original is not None else {"hooks": {}}
    if not isinstance(config, dict) or not isinstance(config.setdefault("hooks", {}), dict):
        raise ValueError("hooks.json must contain a hooks object")
    owned = {target.resolve(), source.resolve()}
    desired = {"type": "command", "command": command, "timeout": 3, "async": False}
    for event in ("PreToolUse", "UserPromptSubmit", "Stop"):
        groups = config["hooks"].setdefault(event, [])
        if not isinstance(groups, list):
            raise ValueError("hook event groups must be arrays")
        placed = False
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                raise ValueError("hook groups must contain a hooks array")
            for handler in group["hooks"]:
                if not isinstance(handler, dict):
                    raise ValueError("hook handlers must be objects")
                path = guard_script(handler.get("command", ""))
                if path in owned:
                    handler.update(desired)
                    if group.get("matcher") in (None, "", "*"):
                        placed = True
        if not placed:
            groups.append({"hooks": [desired.copy()]})
    script_bytes = source.read_bytes()
    old_script = target.read_bytes() if target.exists() else None
    script_changed = old_script != script_bytes
    config_changed = original is None or json.loads(original) != config
    if not script_changed and not config_changed:
        return {"changed": False, "target": target, "backup": None, "review": False}
    backups = codex_home / "hooks/backups"
    backups.mkdir(parents=True, exist_ok=True, mode=0o700)
    backup = Path(tempfile.mkdtemp(prefix="install-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S-"), dir=backups))
    for name, content in (("hooks.json", original), ("quota_guard.py", old_script)):
        if content is not None:
            path = backup / name
            path.write_bytes(content)
            path.chmod(0o600)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if (config_path.read_bytes() if config_path.exists() else None) != original:
        raise ValueError("hooks.json changed during installation; rerun --install")
    if (target.read_bytes() if target.exists() else None) != old_script:
        raise ValueError("quota_guard.py changed during installation; rerun --install")
    try:
        if script_changed:
            with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as f:
                temporary = Path(f.name)
                try:
                    f.write(script_bytes)
                    f.flush()
                    os.replace(temporary, target)
                finally:
                    temporary.unlink(missing_ok=True)
        if config_changed:
            write_json(config_path, config)
    except OSError:
        if script_changed and target.exists() and target.read_bytes() == script_bytes:
            if old_script is None:
                target.unlink()
            else:
                shutil.copy2(backup / "quota_guard.py", target)
        raise
    return {"changed": True, "target": target, "backup": backup, "review": config_changed}


def load_hook_inventory(codex_home, cwd):
    executable = shutil.which("codex")
    if executable is None:
        raise FileNotFoundError("Codex CLI is not on PATH")
    env = dict(os.environ, CODEX_HOME=str(codex_home))
    process = subprocess.Popen([executable, "app-server", "--stdio"], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env, cwd=cwd)
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    buffer = bytearray()

    def request(identifier, method, params):
        process.stdin.write((json.dumps({"id": identifier, "method": method, "params": params}) + "\n").encode())
        process.stdin.flush()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            while b"\n" in buffer:
                line, rest = buffer.split(b"\n", 1)
                buffer[:] = rest
                response = json.loads(line)
                if response.get("id") == identifier:
                    if "error" in response:
                        raise ValueError("Codex rejected hook inventory request")
                    return response["result"]
            if not selector.select(max(0, deadline-time.monotonic())):
                break
            chunk = os.read(process.stdout.fileno(), 65536)
            if not chunk:
                raise OSError("Codex closed its hook inventory connection")
            buffer.extend(chunk)
        raise TimeoutError("Codex hook inventory timed out")

    try:
        request(1, "initialize", {"clientInfo": {"name": "quota-guard-doctor", "version": "1.0"},
                                  "capabilities": {"experimentalApi": True}})
        process.stdin.write(b'{"method":"initialized"}\n')
        process.stdin.flush()
        return request(2, "hooks/list", {"cwds": [str(cwd)]})
    finally:
        selector.close()
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        process.stdin.close()
        process.stdout.close()


def doctor(codex_home, snapshot=None, stop_at=STOP_AT, max_age=MAX_AGE):
    checks = []
    report = check_snapshot(snapshot, stop_at, max_age)
    checks.append((report["decision"] != "unknown", "Widget snapshot", brief(report)))
    target = codex_home / "hooks/quota_guard.py"
    try:
        content = target.read_bytes()
        compile(content, str(target), "exec")
        matches = content == Path(__file__).read_bytes()
        checks.append((matches, "Installed script", "Current version" if matches else "Different version; run --install"))
    except (OSError, SyntaxError, ValueError):
        checks.append((False, "Installed script", "Missing or invalid; run --install"))
    try:
        inventory = load_hook_inventory(codex_home, Path.cwd())
        entries = inventory["data"]
        errors = sum(len(e["errors"]) for e in entries)
        checks.append((errors == 0, "Hook configuration", "Loaded by Codex" if not errors else "Codex reported configuration errors; check hooks.json"))
        hooks = [h for e in entries for h in e["hooks"]
                 if guard_script(h.get("command", "")) == target.resolve()]
        for event in ("preToolUse", "userPromptSubmit", "stop"):
            candidates = [h for h in hooks if h["eventName"] == event]
            if not candidates:
                checks.append((False, event, "Not registered; run --install"))
                continue
            valid = [h for h in candidates if h["enabled"] and h["trustStatus"] in ("trusted", "managed")]
            if not valid:
                checks.append((False, event, "Disabled or needs trust; review and enable it in /hooks"))
                continue
            modes = {"--check", "--brief", "--doctor", "--install", "--print-hooks", "--help", "-h"}
            valid = [h for h in valid if not h.get("async", False)
                     and not modes.intersection(shlex.split(h["command"]))]
            if not valid:
                checks.append((False, event, "Asynchronous or not in hook mode; run --install"))
                continue
            covers_all = event != "preToolUse" or any(h.get("matcher") in (None, "", "*", ".*", "^.*$") for h in valid)
            checks.append((covers_all, event, "Enabled and trusted" if covers_all else "Restricted matcher; run --install for all tool calls"))
    except (OSError, ValueError, KeyError, TypeError):
        checks.append((False, "Hook inventory", "Could not verify with the Codex CLI; check PATH and /hooks"))
    return checks


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="Print quota JSON. Exit 0=above cutoff, 3=cutoff reached, 2=unknown.")
    mode.add_argument("--print-hooks", action="store_true", help="Print hook configuration for this script and its options.")
    mode.add_argument("--brief", action="store_true", help="Print one-line quota status with the same exit codes as --check.")
    mode.add_argument("--install", action="store_true", help="Install this script and merge hook definitions, preserving other hooks.")
    mode.add_argument("--doctor", action="store_true", help="Check the cache, installed script, and actual Codex hook trust and enablement.")
    parser.add_argument("--snapshot", type=Path, help="Use a specific widget-snapshot.json instead of discovering it.")
    parser.add_argument("--stop-at", type=float, default=STOP_AT, help="Inclusive remaining-percent cutoff, default 3.")
    parser.add_argument("--max-age", type=float, default=MAX_AGE, help="Maximum provider reading age in seconds, default 600.")
    parser.add_argument("--state-dir", type=Path, help="Override the directory for private per-turn state.")
    parser.add_argument("--codex-home", type=Path, default=Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")),
                        help="Codex home for --install and --doctor; defaults to CODEX_HOME or ~/.codex.")
    args = parser.parse_args(argv)
    args.codex_home = args.codex_home.expanduser().resolve()
    if not number(args.stop_at) or not 0 <= args.stop_at <= 100:
        parser.error("--stop-at must be between 0 and 100")
    if not number(args.max_age) or args.max_age <= 0:
        parser.error("--max-age must be positive and finite")
    if args.print_hooks:
        print(json.dumps(hook_config(command_for(Path(__file__).resolve(), args)), indent=2))
        return 0
    if args.install:
        target = args.codex_home / "hooks/quota_guard.py"
        try:
            result = install(args.codex_home, command_for(target.resolve(), args))
        except (OSError, ValueError) as error:
            print(f"Installation failed ({type(error).__name__}). Check hooks.json and directory permissions.", file=sys.stderr)
            return 2
        print(("Installed " if result["changed"] else "Already installed: ") + str(result["target"]))
        if result["backup"]:
            print("Backup: " + str(result["backup"]))
        if result["review"]:
            print("In Codex, open /hooks to review, trust, and enable the three quota hooks. Restart the session to load the definitions.")
        else:
            print("Run --doctor to verify hook trust and enablement.")
        return 0
    if args.doctor:
        checks = doctor(args.codex_home, args.snapshot, args.stop_at, args.max_age)
        for ok, name, detail in checks:
            print(("OK " if ok else "FAIL ") + name + ": " + detail)
        return 0 if all(ok for ok, _, _ in checks) else 2
    if args.check or args.brief:
        report = check_snapshot(args.snapshot, args.stop_at, args.max_age)
        print(brief(report) if args.brief else json.dumps(report, indent=2, allow_nan=False))
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
