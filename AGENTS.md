# Repository Guidelines

## Project Structure & Module Organization

`quota_guard.py` is the standalone Python implementation: snapshot parsing, per-turn state, hook handling, installation, diagnostics, and CLI commands. `test_quota_guard.py` contains the automated tests. `README.md` documents installation and behavior; `.github/workflows/test.yml` runs CI on Python 3.8 and 3.13. Keep the distributable script self-contained and standard-library-only.

## Build, Test, and Development Commands

There is no build step. Run commands from the repository root:

```sh
python3 -m unittest -v
python3 quota_guard.py --check
python3 quota_guard.py --brief
python3 quota_guard.py --doctor
```

The first command runs all tests. `--check` prints quota JSON; `--brief` prints one-line status. Both exit with `0` above the cutoff, `3` at the cutoff, or `2` when quota is unknown. `--doctor` checks the cache, installed script, and Codex hook inventory; it requires `codex` on PATH.

Test installation in an isolated directory:

```sh
python3 quota_guard.py --install --codex-home /tmp/codex-guard-dev
```

## Coding Style & Naming Conventions

Use four-space indentation, `snake_case` for functions and variables, `CapWords` for classes, and uppercase constants. Maintain Python 3.8 compatibility. Follow the existing concise function style and validate external JSON at boundaries. No formatter or linter is configured; check whitespace with `git diff --check`.

## Testing Guidelines

Use standard-library `unittest`, with methods named `test_<behavior>`. Use synthetic snapshots, temporary directories, and mocked external services. Cover cutoff boundaries, stale or ambiguous readings, override isolation, CLI exit codes, and preservation of existing hooks. Add behavioral tests for changes; no numerical coverage target is configured.

## Commit & Pull Request Guidelines

Use short imperative subjects describing behavior, following history such as `Gate Codex tool calls using CodexBar widget cache`. Keep commits scoped. PRs should explain the problem and resulting behavior, link relevant issues, and list validation commands and results. Call out changes to quota policy, installation, or trust handling.

## Security & Configuration

Default policy blocks at or below 3% remaining or when readings cannot be verified; the freshness limit is 600 seconds. Ordinary checks must read the cache without CLI or network fallback. Preserve unrelated hooks and leave trust approval explicit. Keep overrides confined to one turn. Never commit real snapshots, credentials, prompt contents, or local hook backups.
