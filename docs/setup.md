# Setup guide

Install, activation, response modes, hook checks, updates and uninstall. Back to the [README](../README.md).

## Requirements

Claude Code and/or Codex, `python3` 3.9+ and `git`. macOS, Linux and Windows.

**Windows:** hooks run `python3`, so that exact command must work. Python from the python.org install manager or the Microsoft Store provides it; the classic python.org installer only adds `python` and `py`. Claude Code on Windows runs hooks through Git Bash, which comes with Git for Windows. `honey` is installed as a small launcher file at `~/.local/bin/honey` rather than a symlink, so you don't need Developer Mode. Not yet supported on Windows: RSI's autonomous skill patching (drafting and evaluating skill changes on its own); hooks, install, guard, task evidence and learnings work.

## Install

**One line.** Clones (or updates) `~/.honey-stack/repo` (override with `HONEY_STACK_DIR`), shows a dry run, then prints the command that applies it:

```bash
curl -fsSL https://raw.githubusercontent.com/umararshad4/honey-stack/main/install.sh | bash
```

On Windows, in PowerShell:

```powershell
irm https://raw.githubusercontent.com/umararshad4/honey-stack/main/install.ps1 | iex
```

**Manual:**

```bash
git clone https://github.com/umararshad4/honey-stack.git && cd honey-stack
python3 bin/honey install --dry-run && python3 bin/honey install
```

Either way, honey installs into both agents, puts `honey` on PATH and turns off competing always-on plugins. Every change is reversible. With many other skills installed, `honey install --skill-budget 0.02` gives skill descriptions more room (Claude only).

**Plugin only** (pin a release with `marketplace add … --ref vX.Y.Z`):

```bash
claude plugin marketplace add umararshad4/honey-stack && claude plugin install honey@honey-stack
codex plugin marketplace add umararshad4/honey-stack && codex plugin add honey@honey-stack
```

`honey` isn't on PATH here: run it as `python3 <plugin dir>/bin/honey doctor` (same for `uninstall`).

## Activate

- **Claude Code:** start a new session or run `/reload-plugins`.
- **Codex:** open `/hooks` and trust the honey entries.
- **Check:** `honey doctor`.

`honey install` also adds a small managed activation block to global `~/.codex/AGENTS.md` and `~/.claude/CLAUDE.md`, preserving personal instructions. It loads `honey bootstrap --agent codex` (or `claude`) at the first user turn. This read-only fallback provides the workflow and visible indicator when startup context is absent; it never manufactures hook events. `🍯 HONEY · … · hooks unverified` means instructions loaded but native event delivery has not been observed. Uninstall removes the managed block while preserving later personal edits.

## Check hooks

The **`🍯 HONEY`** prefix indicates injected rules. To check whether the current session actually receives hooks, use `honey doctor --agent codex --session <session-id> --json` (or `--agent claude`). Installation, enablement, recent delivery and payload coverage are reported separately; a prefix or smoke test is not proof of enforcement. For Codex, `hook_trust` reads the native hook registry: `review_required` means the installed plugin needs review in `/hooks`, and `unknown` means the registry could not be checked. Saved approvals alone are not proof of current trust.

## Response modes

Choose one response style. Send it as the **whole message**. Add `default` to save it for new sessions (`honey direct default`).

| Mode | Does | Use when |
|---|---|---|
| `honey direct` *(default)* | Straight to the point, concise responses | You want the answer and essential details |
| `honey explain` | Comprehensive explanations with useful context and examples | You want to understand the reasoning |
| `honey balance` | Moderate detail, neither comprehensive nor too brief | You want the answer with key reasoning |
| `honey off` · `honey on` | Pause or resume for this session (safety guard stays on) | A task where honey's rules get in the way |

The indicator shows the active choice: `🍯 HONEY · direct`. Use `honey status` to check it. These modes change response detail; task sizing, verification, and safety controls apply in every mode. Explicit requests for detail take priority. Saved legacy `terse` settings read as `direct`, and `normal` as `balance`; the old scope/process choices are retired. Saving a new default replaces the old `modes` config without changing other settings.

Also: `remember: when X, Y` teaches a rule now. Scopes: `(project)` `(claude)` `(codex)` `(skill:<name>)`, e.g. `remember (project): …`. `rsi off` · `rsi on` pauses learning for this session.

## Update and uninstall

honey checks for updates at session start, at most once an hour. For local Git marketplaces it fetches the newest `vX.Y.Z` release tag reachable from `main`, fast-forwards the checkout, and refreshes the installed plugin copies. It leaves local changes, feature branches and diverged checkouts untouched, reporting them as skipped even when they already contain the latest release tag. If a plugin refresh fails, it retries on a later session; the update notice appears after the refresh succeeds. The new version loads in a subsequent session. Remote plugin marketplaces use the agent's own marketplace update commands and follow the marketplace's configured ref. Turn updates off with `{"autoupdate": false}` in `~/.honey/config.json`. Codex checks the installed plugin contents as well as hook commands. Any plugin update, including an evaluated skill patch, can require fresh review in `/hooks`; Honey does not approve changed plugin code automatically.

For a one-time agent-assisted update and Honey-only hook review in Codex, say `update hooks` or `$honey update hooks`. This authorizes the agent to review and approve Honey for that run, check the release immediately, and report the result. It does not approve unrelated hooks or future releases.

```bash
honey update                                   # update now; then /reload-plugins in Claude
honey autoupdate --now                         # check release tags without waiting for the hourly interval
honey uninstall --dry-run && honey uninstall   # keeps ~/.honey; add --purge to delete it
```

Plugin-only installs:

```bash
claude plugin marketplace update honey-stack && claude plugin update honey@honey-stack
codex plugin marketplace upgrade honey-stack && codex plugin add honey@honey-stack
```
