---
schema_version: 1
handoff_id: 2dd7
parent_handoff_ids: []
lineage: none
chain: [standalone-ba22]
repo: hermes-agent
workspace: main
branch: main
head_sha: ad9678e638e8e1d77e2175b6eb6aab82611f6280
created_at: 2026-10-08T10:48:00-0400
writer: claude-code
---
# Handoff — Discord silent on prompts: config big-int rounding, fork fix, upstream PR

## The Goal

1. Make the Hermes Discord bot answer prompts in `#bot-inbox` again (it received gateway start/stop notices but ignored every prompt). **Done, confirmed by djbclark 2026-10-08.**
2. Fix the root cause so it cannot recur, in the fork and upstream. **Fork: done and live. Upstream: issue filed; PR branch pushed, PR itself waits on a browser click (see Where We're Going 1).**

## Where We Are

1. Root cause: `platforms.discord.extra.free_response_channels` in `~/.hermes/config.yaml` had been rewritten from `1535369580753592401` to `1535369580753592300` — the value JavaScript produces for that integer (`node -e 'console.log(1535369580753592401)'`). `GET /api/config` emitted the bare YAML int as a JSON number; the desktop app's debounced `PUT /api/config` autosave (and the web dashboard) wrote the rounded double back. Broken since at least 2026-09-29 (seen in `~/.hermes/sessions/request_dump_20260929_200307_*`). With no matching free-response channel the adapter required an @mention and dropped everything else at DEBUG level — there is no `inbound message: platform=discord` line anywhere in `~/.hermes/logs/gateway.log`.
2. Config fixed 2026-10-06 15:27: value written as the quoted string `'1535369580753592401'`; `hermes gateway restart` (launchd job `com.stayturgid.hermes-gateway`, now PID 99342). Pre-edit copy: `~/.hermes/backups/config/config.yaml.pre-discord-fix.20261006-152740` (kept on purpose, djbclark's choice).
3. Fork fix, live: commit `ad9678e638` on `main` (pushed to `origin` = github.com/djbclark/hermes-agent). `_JS_MAX_SAFE_INTEGER` + `_stringify_unsafe_ints()` in `hermes_cli/web_server_config.py`, applied in `_normalize_config_for_web`, so `GET /api/config` serves ints > 2**53 as strings; ruamel quotes them on save. Processes that serve `/api/config` were restarted onto it: dashboard launchd job `com.djbclark.hermes-dashboard` (kickstart) and the desktop app (`open -a .../release-new/mac-arm64/Hermes.app`, whose embedded `hermes serve --port 0` child is the autosave writer). The gateway does **not** serve `/api/config`; it needed no restart for this.
4. Upstream: issue https://github.com/NousResearch/hermes-agent/issues/135122 (sibling of #55314, same rounding on the tool-argument path). PR branch `fix/config-bigint-strings` = `e4611afe71`, a cherry-pick of the fix onto `upstream/main` (`25a71a744c`) with the test moved into a new file; pushed to `origin`. Worktree: `~/src/hermes-worktrees/fix-config-bigint`.
5. Tests: fork — `tests/hermes_cli/test_web_server.py -k "TestModelContextLength or normalize or denormalize or config"`: 25 passed. PR branch — `test_web_server_config_bigint.py` + `TestModelContextLength`: 6 passed; `python scripts/check --base upstream/main`: 11 checks ok. Full `pytest tests/ -q` **not** run (live gateway on this machine; the PR checklist says so honestly).
6. Durable notes written: Basic Memory `main` → `~/ops/site-private/memory/reference_hermes_config_bigint_rounding.md` (committed, pushed, indexed in its `MEMORY.md`); Claude auto-memory `project-hermes-config-js-bigint-rounding.md` in this project's memory dir.
7. Fork `main` is clean (`apps/desktop/release-new/` untracked is the running desktop build, not ours).

## What We Tried

1. `gh pr create` (GraphQL) → `djbclark does not have the correct permissions to execute CreatePullRequest`; `gh api -X POST repos/NousResearch/hermes-agent/pulls` → 404. Token has `repo` scope, so the likely cause is the NousResearch org restricting third-party OAuth apps (gh's). Not retried with a PAT; djbclark chose the browser route instead.
2. `launchctl kickstart -k gui/501/application.com.nousresearch.hermes.*` to restart the desktop app: killed it but returned `102: Operation not supported on socket` and did not relaunch — GUI-app launchd labels do not support kickstart. `open -a <Hermes.app>` relaunched it.
3. `python scripts/check` without `--base` compared against fork `origin/main`, which lags upstream, and reported dozens of false health failures. `--base upstream/main` gives the real result; the one true failure was adding a test to `tests/hermes_cli/test_web_server.py` (6027 lines, over the 2000-line cap, may only shrink) → moved the test to `tests/hermes_cli/test_web_server_config_bigint.py`.
4. Multi-line `sd` replacement on the memory note silently matched nothing (line-wrap differences); a Python `re` edit with a single-line anchor worked. Check `rg` for the new text after any `sd`.

## Key Decisions

1. **Fix server-side (stringify in `_normalize_config_for_web`)**, rejected: BigInt-aware JSON parsing in both SPAs (two code paths, number inputs still break on strings) and quoting IDs in `hermes config set` (does not protect existing configs or hand-edited bare ints).
2. **Quoted string, not int, on disk** for the Discord ID — survives the JS round-trip; adapters stringify anyway (`_gate_csv_set`).
3. **Fork `main` keeps the test inside `test_web_server.py`** (commit `ad9678e638`) while the PR branch has it in a new file (`e4611afe71`). Accepted divergence; it will surface as a trivial conflict on the next upstream merge (see Where We're Going 3).
4. **Not taking over the Telegram `/helm` `/steps` `/skill` regression** reported by peer session `djbclark-ade-8d`: its `tools.override` diagnosis did not verify (the deny line is logged for every user plugin at plugin discovery, never at the gateway's own startup; `register_command` is not gated by it; `plugins:` config byte-identical to the Oct 6 backup), and Hermes CLI session `20261008_100648` was editing `~/.hermes/plugins/skill-slash/__init__.py` at 10:07. Peer informed via SendMessage; djbclark chose to leave it with that session.
5. Restarted the gateway (Oct 6) and dashboard/desktop (Oct 8) without asking first — idle at the time, disclosed in the /loose audit.

## Evidence & Data

1. `~/.hermes/logs/gateway.log`: Discord connects fine every restart (`Connected as djbclark-hermes#2870`), periodic `latency_exceeded` reconnects and slash-command-sync 429s are noise, unrelated.
2. `~/.hermes/logs/agent.log` 2026-10-08 00:31:47: `capability_check plugin={gateway-restart,orca-status,skill-slash} capability=tools.override decision=deny` — three lines from one plugin-discovery pass, not a skill-slash-specific event.
3. Health-check baseline rule: `scripts/code_health/config.py` lines 13-17 (reads `origin/main` locally; CI reads merge-commit first parent).
4. `hermes_cli/plugins.py` ~575-598: `_tool_override_allowed` is about overriding built-in tools; bundled plugins trusted, others need `granted_capabilities` or legacy `allow_tool_override: true`.

## Operator Feedback

1. "but search for existing issues first" (before filing upstream) — done: 9 search terms across issues and PRs; nothing covered the config-API path.
2. Chose: restart desktop app now; keep the pre-fix backup; open the PR now (browser route after gh refused); leave skill-slash to the session already on it; `/handoff` to close.

## Where We're Going

1. **Open the upstream PR.** Compare page: https://github.com/NousResearch/hermes-agent/compare/main...djbclark:hermes-agent:fix/config-bigint-strings?expand=1 — the full PR body (checklist filled, "Fixes #135122") is at the scratchpad of the writing session and reproduced in `git show e4611afe71` plus issue #135122; regenerate it from those if the clipboard is gone. Check first: `gh pr list --repo NousResearch/hermes-agent --head djbclark:fix/config-bigint-strings` (empty = not yet opened).
2. After the PR exists, drop a one-line comment on #135122 linking it; then watch for maintainer review (`gh pr view <n> --repo NousResearch/hermes-agent --comments`).
3. On the next upstream merge into the fork (playbook: Claude auto-memory `project-hermes-fork-upstream-merge.md`), expect a trivial conflict in `tests/hermes_cli/test_web_server.py` around `test_normalize_stringifies_ints_beyond_js_safe_range`: keep upstream's `test_web_server_config_bigint.py`, delete the fork's in-class copy. Preserve `_stringify_unsafe_ints` until upstream ships it.
4. Remove the worktree once the PR is merged or abandoned: `git -C ~/.hermes/hermes-agent worktree remove ~/src/hermes-worktrees/fix-config-bigint && git -C ~/.hermes/hermes-agent branch -D fix/config-bigint-strings`.
5. Not ours, for awareness only: Telegram `/helm` `/steps` `/skill` — find session `20261008_100648` (`session-finder` skill) before touching `~/.hermes/plugins/skill-slash/`.

## Quick Start

```bash
cd ~/.hermes/hermes-agent && git status -sb && git log --oneline -3
gh pr list --repo NousResearch/hermes-agent --head djbclark:fix/config-bigint-strings
gh issue view 135122 --repo NousResearch/hermes-agent --comments | head -40
rg -n free_response_channels ~/.hermes/config.yaml        # expect the quoted ID
rg -c 'inbound message: platform=discord' ~/.hermes/logs/gateway.log   # >0 now that it answers
cd ~/src/hermes-worktrees/fix-config-bigint && PYTHONPATH=. ~/.hermes/hermes-agent/.venv/bin/python scripts/check --base upstream/main
```
