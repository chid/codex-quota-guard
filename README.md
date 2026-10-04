# Codex quota guard

A Codex tool-use hook and callable quota checker backed by [CodexBar](https://github.com/steipete/CodexBar)'s local macOS widget snapshot. Python 3.8 or later, standard library only. Quota checks read the cache without launching the CodexBar CLI or making network requests.

By default, the guard denies supported tool calls when either the session or weekly quota is at or below **3% remaining**. Missing, invalid, ambiguous, expired, or stale readings also deny tool use. It reads the widget file on every invocation.

## Call it anytime

```sh
python3 quota_guard.py --check
python3 quota_guard.py --brief
```

`--check` prints JSON containing remaining percentages, reset times, the provider's update timestamp, and a decision. It does not print account identities or copy the raw widget snapshot. `--brief` prints a single line suitable for terminal status displays:

```text
Session 88% | Weekly 94% | Updated 2m ago
```

The brief output adds `STOP` at the cutoff or `UNAVAILABLE` when the reading cannot be verified. Both commands use the same exit codes:

| Exit code | Meaning |
| --- | --- |
| 0 | Both quota windows are above the cutoff |
| 3 | A valid quota window is at or below the cutoff |
| 2 | Current quota cannot be established |

Gate a command using the exit code:

```sh
python3 quota_guard.py --check && your-command
```

Each invocation reads the file again. To check repeatedly, invoke the command at each checkpoint. There is no background watcher.

## Install as a Codex hook

```sh
git clone https://github.com/chid/codex-quota-guard.git
python3 codex-quota-guard/quota_guard.py --install
```

The installer copies the script to `~/.codex/hooks/quota_guard.py` and merges `PreToolUse`, `UserPromptSubmit`, and `Stop` definitions into `~/.codex/hooks.json`. It preserves unrelated hooks and configuration, backs up existing files under `~/.codex/hooks/backups/`, and refuses malformed configurations. Repeating an unchanged installation creates no writes or backups. Run the installer from an updated checkout to upgrade.

Start a new Codex session and use `/hooks` to review, trust, and enable the three definitions. The installer does not approve trust or change enabled states. See the [official hook documentation](https://learn.chatgpt.com/docs/hooks).

To target another Codex home, set `CODEX_HOME` or pass `--codex-home /path/to/codex-home`. Snapshot, threshold, freshness, and state-directory options passed to `--install` are included in the hook commands. Relative file paths are resolved at installation time.

For a manual installation, `--print-hooks` prints definitions pointing to the script you invoke. Merge those definitions into your hooks configuration.

Add this to your global `~/.codex/AGENTS.md` so the agent stops after a denial instead of attempting another tool path:

```markdown
When the quota guard denies a tool call, end the turn with a brief progress
summary and the reported quota/reset information. Do not retry, wait for
an automatic reset, switch accounts, or bypass the guard with other tools.
```

## Diagnose installation

```sh
python3 ~/.codex/hooks/quota_guard.py --doctor
```

The doctor checks the snapshot, compares the installed script with the version you invoke, and asks the local Codex CLI for its actual hook inventory. It reports missing, disabled, untrusted, modified, asynchronous, or restricted quota hooks. Put `codex` on `PATH` for this check. No model task is started, and the doctor makes no configuration changes.

Run `--doctor` from the repository checkout to detect an outdated installed copy. Exit code `0` means the checks passed; `2` means a check failed. A valid reading at the cutoff appears as `STOP` but does not indicate a broken installation. Use `--check` or `--brief` to gate work on quota.

The inventory reflects the configuration Codex loads from disk. Restart an existing session after hook configuration changes so that it loads the same definitions.

## Override one turn

Include this exact standalone line in your user prompt:

```text
Ignore quota cutoff for this task
```

The `UserPromptSubmit` hook records the override for that session and turn. The line must match exactly, including capitalization and no trailing punctuation. Ordinary continuation requests, quoted examples, and fenced code examples do not count. Other sessions and subsequent turns retain the cutoff. A denied turn remains blocked and its `Stop` hook rejects automatic continuation.

## Source and freshness

The default source is:

```text
~/Library/Group Containers/*com.steipete.codexbar/widget-snapshot.json
```

The checker selects the unique `codex` provider entry and reads the `session` and `weekly` usage rows and their underlying rate windows. Older snapshots exposing `primary` and `secondary` windows directly also work. Ensure CodexBar displays the same account you use in Codex.

The default maximum age is **600 seconds**, allowing two five-minute refresh intervals. Freshness comes from the Codex entry's `updatedAt`, not the overall file's timestamp or `generatedAt`. Keep CodexBar running with a refresh interval shorter than the age limit. Stale data deny tool use; there is no automatic CLI fallback.

```sh
python3 quota_guard.py --check --stop-at 5 --max-age 300
python3 quota_guard.py --check --snapshot /path/to/widget-snapshot.json
python3 quota_guard.py --install --stop-at 5 --max-age 300
```

`--print-hooks` includes the supplied options in each hook command. `--state-dir` changes the directory used for private override and denial state. The default is `$XDG_CACHE_HOME/codex-quota-guard`, or `~/.cache/codex-quota-guard` when unset. State files have mode `0600` and the root directory has mode `0700`. Audit files contain only hook identifiers, decisions, and quota metadata, not prompt or tool input text.

## Limits

This is a guardrail, not a billing or security boundary. Cached reporting can lag and overshoot a cutoff. Codex does not intercept every tool path: hosted web searches and already-running commands are examples. Hook errors or runtime timeouts can also fail open even though the script emits a denial for failures it can handle. The global instructions remain necessary.

The checker covers session and weekly quotas, not code-review credits, model-specific allowances, or API billing. It cannot establish that a different account has quota available.

## Verify

```sh
python3 -m unittest -v
```

Tests use synthetic widget snapshots. No credentials, live snapshots, or account quota data are included in the repository.
