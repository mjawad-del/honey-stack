# 🍯 honey-stack

**An always-on workflow layer for Claude Code and Codex that learns from every session.**

[![ci](https://github.com/umararshad4/honey-stack/actions/workflows/ci.yml/badge.svg)](https://github.com/umararshad4/honey-stack/actions/workflows/ci.yml)
[![release](https://img.shields.io/github/v/release/umararshad4/honey-stack)](https://github.com/umararshad4/honey-stack/releases)
[![license](https://img.shields.io/badge/license-MIT-blue)](LICENSE)
[![docs](https://img.shields.io/badge/docs-honey--stack.vercel.app-f3b544)](https://honey-stack.vercel.app)

honey-stack combines the best of [Superpowers](https://github.com/obra/superpowers), [Ponytail](https://github.com/DietrichGebert/ponytail), [Matt Pocock's skills](https://github.com/mattpocock/skills), [pstack](https://github.com/cursor/plugins/tree/main/pstack) and [gstack](https://github.com/garrytan/gstack) in one small layer, then adds **RSI** (recursive self-improvement): it learns from your corrections and can apply validated improvements on its own.

- **Small:** ~2 KB of always-on rules, not ~25 KB of stacked prompts.
- **Disciplined:** sizes each task, makes the smallest complete change and proves it before "done".
- **Self-improving:** turns your corrections into rules and evaluated skill patches.
- **Local and reversible:** Honey state stays in `~/.honey/`; `honey uninstall` restores everything. External history integrations are optional.

## Quick start

Needs Claude Code and/or Codex, `python3` 3.9+ and `git` on macOS, Linux or Windows.

**1. Install.** This clones the repo and shows a dry run; then run the install command it prints.

```bash
curl -fsSL https://raw.githubusercontent.com/umararshad4/honey-stack/main/install.sh | bash
```

Windows (PowerShell): `irm https://raw.githubusercontent.com/umararshad4/honey-stack/main/install.ps1 | iex`

**2. Activate.** Claude Code: start a new session or run `/reload-plugins`. Codex: open `/hooks` and trust the honey entries.

**3. Check.** Run `honey doctor`. Replies now start with `🍯 HONEY · direct`.

Manual and plugin-only installs, hook troubleshooting and the `hooks unverified` label are covered in the [setup guide](docs/setup.md).

## Everyday use

Work as usual: honey sizes each task and picks the right skill. To steer it, send one of these as the whole message:

| Say | What it does |
|---|---|
| `honey direct` · `honey balance` · `honey explain` | Concise (default), moderate or detailed replies. Append `default` to save. |
| `honey off` · `honey on` | Pause or resume honey for this session. The safety guard stays on. |
| `honey status` | Show the active mode. |
| `remember: when X, Y` | Teach a rule now. Scope it with `(project)`, `(claude)`, `(codex)` or `(skill:<name>)`. |
| `rsi off` · `rsi on` | Pause or resume learning for this session. |
| `update hooks` | Update now and review honey's hooks in Codex. |

Modes change how much is explained, not how carefully work is done. Sizing, verification and safety apply in every mode.

### Intent loop

honey checks that it understood you before it acts, and that it finished everything you asked for:

- **Asks when a misread would mean a redo.** If a request has two readings (an answer or an action, a different scope or target), honey asks one question with its recommended answer first, using `AskUserQuestion` in Claude Code or `request_user_input` in Codex. Otherwise it goes ahead and tags its guesses `(my guess)`.
- **Tracks every ask.** A request with a numbered or bulleted list ends with `Asks: 1 done · 2 partial (why)`. If an edit turn skips that line, the next reply gets one reminder.
- **Answers "is everything done?"** A question like "all the changes I asked done?" brings back this session's ask log so each item gets a status. The log is also restored after context compaction.
- **Respects "just asking".** "I'm just asking…" gets an answer, not an edit.
- **Remembers your terms.** "I told you ship means merged too" or "by staging I mean the QA cluster" is saved as a project learning (undo: `honey rsi forget <id>`). `rsi off` turns saving off.

Nothing in the loop blocks. Each hint is added only when its pattern matches.

## Skills

**Flow:** grill → plan → execute → verify → review → ship. Bugs start at `debug`.

| Invoked | Skills |
|---|---|
| **Automatic** | `grill` `plan` `execute` `debug` `tdd` `verify` `review` `ship` `deploy` `test-design` `tooling` `database` `perf` `observability` `api-design` `upgrade` `incident` `frontend` `stageshow` `qa` `security` `docs` |
| **On demand** | `honey` `rsi` `unslop` `reverse-engineer` `handoff` `prototype` `architecture` `domain` `teach` `write-skill` `principles` `opensrc` `project-verify` `triage` `research` `statechart` `gain` |

Call a skill with `/honey:<skill>` in Claude Code or `$<skill>` in Codex. See [docs/skills.md](docs/skills.md) for focused workflows and browser QA.

## Browser QA with agent-browser

When the agent steps through a page itself (a quick "check this page" pass, a bug repro, or exploring before you write a test), `qa` uses [agent-browser](https://github.com/vercel-labs/agent-browser) if it's on your PATH. It returns compact accessibility snapshots with `@e` refs, so each step costs the agent far less context than Playwright MCP. Existing e2e suites still run first and stay the regression proof. Without agent-browser, `qa` uses Playwright MCP or e2e as before.

```bash
npm i -g agent-browser && agent-browser install   # optional
honey doctor                                     # "Browser QA" shows the detected version
```

Measured on the same 12-step QA flow, 10 rounds each ([report](evals/agent-browser/reports/2026-11-14-agent-qa.md)):

| Tool | Median wall time | Tool calls | Text the agent reads | ≈ Tokens |
|---|---:|---:|---:|---:|
| **agent-browser `snapshot -i`** | 3.13s | 12 | **1,078 chars** | **≈270** |
| Playwright MCP 0.0.85 | 3.13s | 13 | 4,922 chars | ≈1,231 |

Wall time ties; the agent reads 4.6× less. The gain is cheaper, quicker model turns and longer sessions before context fills, not a faster browser. Rerun it with `npm ci && npm run benchmark` in `evals/agent-browser`.

## RSI: how it learns

1. **Observe:** hooks log redacted corrections, "still broken" moments and which skills fired.
2. **Reflect:** after a session with signal, a small model (Haiku, ~$0.002) proposes evidence-backed `WHEN → THEN` rules.
3. **Curate:** a rule goes live once two sessions back it, goes stale after 30 days and is archived after 90.
4. **Apply:** learned rules load automatically. Instruction edits, skill patches and guards wait for `/rsi review`, or apply after validation when autonomy is on.

| Command | Use it to |
|---|---|
| `/rsi status` | See what's running and queued. |
| `/rsi review` | Approve or reject pending proposals. |
| `/rsi autonomy on` · `off` | Apply validated changes automatically, or go back to review. |
| `/rsi coach` | Get an evidence-based report on what's improving. |
| `/rsi why <id>` · `forget <id>` | Inspect a rule or reject it for good. |

In Codex, use `$rsi …`. Tiers, budgets, autonomy and GitHub publishing are covered in [docs/rsi.md](docs/rsi.md).

## Configure

All keys in `~/.honey/config.json` are optional:

```json
{
  "mode": "direct",
  "autoupdate": true,
  "budgets":  {"enabled": true, "reflection_usd": 0.15, "weekly_usd": 1.5, "monthly_usd": 5},
  "autonomy": {"min_sessions": 2, "holdout_rate": 0}
}
```

**Privacy:** Honey has no telemetry and stores redacted learning snippets of up to 300 characters locally.

## Update and uninstall

honey checks for a new release at session start, at most once an hour.

```bash
honey update                                   # update now; then /reload-plugins in Claude
honey uninstall --dry-run && honey uninstall   # keeps ~/.honey; add --purge to delete it
```

## Docs

**Documentation site: [honey-stack.vercel.app](https://honey-stack.vercel.app)** — every feature, explained with interactive diagrams. The guides below are the in-repo source.

| Guide | Covers |
|---|---|
| [Setup](docs/setup.md) | Install options, activation, hook checks, modes, updates |
| [Skills](docs/skills.md) | Focused workflows, browser QA with e2e, WebMCP and agent-browser |
| [RSI](docs/rsi.md) | Tiers, autonomy, budgets, publishing improvements |
| [Task evidence](docs/task-evidence.md) | `honey task` scope and proof of checks |
| [Development](docs/development.md) | Tests, CI and automatic releases |
| [Skill style](docs/skill-style.md) | Writing conventions for skills |

MIT © King Umar · [Changelog](CHANGELOG.md) · [Credits and licenses](THIRD_PARTY.md)
