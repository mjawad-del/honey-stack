# Changelog

All notable changes to honey-stack. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/):

- **MAJOR**: breaking changes to hook commands (Codex would need to re-trust), the `~/.honey` data layout, or the CLI
- **MINOR**: new skills or RSI capabilities
- **PATCH**: fixes

Successful main CI automatically versions and publishes releases. Manual releases remain available with `python3 scripts/release.py X.Y.Z --push`.

## [Unreleased]

### Fixed
- rsi: new-skill suggestion issues no longer publish the user's prompt keywords in their titles.
- rsi: automatic delivery merges only PRs with an approving review; an empty review decision now waits.
- rsi: failed reflections no longer consume the daily reflection cap, have their own cap, fall back to the other installed engine, and stop retrying a session after three consecutive failures.
- rsi: `remember:` notes are queued instead of lost when a background job holds the lock past the hook timeout.
- rsi: redaction covers `NAME_API_KEY=…`, `DATABASE_PASSWORD=…`, "password is …" and Bearer tokens.
- install: `honey uninstall` no longer crashes on automatic toolkit repair records, which now carry a summary.
- rsi: autonomous mode no longer edits or removes user-written lines in CLAUDE.md / AGENTS.md.

## [0.18.3] - 2026-11-07

### Changed
- fix: answer-first rules, worktree-safe evidence, quieter feedback signals (#48)

## [0.18.2] - 2026-11-07

### Changed
- fix(hooks): make every honey rule advisory, never blocking (#47)

## [0.18.1] - 2026-11-03

### Fixed
- Keep learned guard warnings from becoming hard edit or command denials on Codex, and clarify that task inspection records do not restrict authorized edits.

## [0.18.0] - 2026-11-03

### Added
- Opt-in private Codex conversation capture with a per-user macOS observer, durable delivery records, explicit upload status and workspace-level disable controls.
- Capture works in plain workspaces as well as Git repositories and excludes pre-activation history, inherited subagents, injected instructions, reasoning, tools and repository files.

## [0.17.2] - 2026-11-03

### Fixed
- Keep UI-removal requests on ordinary verification while recognizing explicit application uninstalls.
- Reconcile published suggestion issues before automatic improvement work, preserving closure reasons without claiming the suggestion was implemented.

## [0.17.1] - 2026-11-03

### Fixed
- Prevent inferred inspection mode from blocking authorized edits or execution evidence.

## [0.17.0] - 2026-11-01

### Added
- Optional Delta history integration for Claude Code and Codex, with scoped search, bounded decision recall and fresh summary checks.
- Local handoff references that verify Git state and saved evidence, plus explicit capture-policy previews and separate upload readiness.
- Delta installation and usage guides, CLI regression tests and synthetic before/after QA.

## [0.16.4] - 2026-11-01

### Changed
- Merge pull request #29 from umararshad4/fix/intent-defaults-to-execute
- fix(tasks): honor negated edit verbs and keep read-only on task set
- fix(tasks): stop treating ordinary requests as read-only

## [0.16.3] - 2026-11-01

### Changed
- Merge pull request #28 from umararshad4/t3/docs-site-link
- docs: link the documentation site

## [0.16.2] - 2026-11-01

### Changed
- Merge pull request #27 from umararshad4/docs/compact-readme
- docs: compact README into a quick-start guide

## [0.16.1] - 2026-11-01

### Changed
- Merge pull request #25 from umararshad4/t3/fix-ship-github-issues
- fix(workflow): address gating and improvement backlog

## [0.16.0] - 2026-11-01

### Changed
- QA now defaults to the tester-army/e2e CLI and project MCP workflow, with complete upstream command references and focused, cache-aware reruns.
- Browser QA and verification discover WebMCP tools, with a same-session e2e adapter, isolated Chromium runner, explicit fallback and independent UI-proof requirements.
- QA replaces eligible setup with WebMCP, reuses verified sessions and tests, and limits separate contract checks to relevant work. Speed comparisons require matched workloads and repeated runs.
- QA can batch reviewed same-page WebMCP setup calls, builds complete case matrices before running, and avoids duplicate setup screenshots while retaining failure evidence.
- Fast QA uses native e2e reports, bounded isolated workers and deliberate tracing, and avoids polling values already read after confirmed readiness.
- WebMCP's browser launcher cancels its unused shutdown timer instead of adding two seconds of idle exit time. Fast QA distinguishes current-state assertions from negation stability windows and balances file/target units before adding workers.
- WebMCP QA runs on Playwright's headless Chromium build and automation switches, starts e2e while its browsers boot, gives each worker its own browser through an e2e browser provider, and enables Node's compile cache. The matched 24-case run is about 3.3x faster than the previous WebMCP flow.

## [0.15.0] - 2026-10-31

### Changed
- Merge pull request #23 from umararshad4/t3/clarify-rsi-improvement-delivery
- feat(skills): add verification, triage, and research workflows

## [0.14.0] - 2026-10-31

### Added
- Stageshow guides animation implementation and includes a React kit for product illustrations and cursor demos.

## [0.13.0] - 2026-10-29

### Added
- Opt-in `honey rsi publish automatic` completes validated skill delivery through exact-head CI-gated merge, release detection and retryable installation while preserving private local history.

## [0.12.0] - 2026-10-29

### Added
- `honey task run` executes real verification commands and records snapshot-bound evidence on hosts that hide inner tool events.
- Opt-in `honey rsi publish on/off/status/run` publishes pending suggestions as GitHub issues and applied, evaluated skill changes as PRs, with duplicate prevention, exact-head receipts and retryable failures. Private session evidence, evaluation history and local preferences stay local.
- Automatic semantic-version releases after successful main CI, with atomic commit/tag pushes, stale-run protection and retryable GitHub publication.
- Session-specific hook health separates installation, enablement, delivery and supported payload coverage.
- Task contracts record scope, workspace and operation-specific checks; completion reminders use fresh observed results instead of receipt wording alone.
- Bounded task/session tool metrics and coach denominators for repeated reads/checks, failures, unknown results and reported durations.
- Maintained behavioral evaluation cases, fixture integrity checks and exact candidate checks before skill patches apply.

### Changed
- Replaced scope/process/voice mode choices with `direct`, `explain`, and `balance` response styles. The indicator and status line show the active style; existing saved voice settings are read compatibly.
- Feedback attribution uses task identity and explicit skill references, preserving uncertainty for ambiguous corrections, requirement changes, preferences and environment failures.
- Strict test-first guidance applies to code; inspection stays read-only and system tasks use resulting-state checks.

### Fixed
- Recognize direct “make it” implementation requests after inspection without requiring a repeated confirmation phrase.
- Local marketplace auto-updates now fetch release tags into the source checkout and refresh installed plugins. Failed refreshes remain pending for retry and do not report a successful update.
- Recognize modern shell/read/edit envelopes, reject conflicting success metadata, and preserve missing or opaque outcomes as unverified.
- Keep failed, incomplete, stale and previous-task checks from verifying current work; permit explicit workspace binding for sessions started outside the project.
- Isolate direct test-helper imports from real RSI state and paid providers.

## [0.11.0] - 2026-10-19

### Added
- Auto-update: each new session starts a throttled background job (`honey autoupdate`) that moves a git checkout to the newest `vX.Y.Z` release tag, or refreshes plugin-only installs through the agents' plugin CLIs. Dirty or diverged checkouts are skipped, the next session shows a one-line notice, and `{"autoupdate": false}` in `~/.honey/config.json` turns it off.

## [0.10.0] - 2026-10-19

### Added
- Opt-in autonomous RSI applies validated instruction edits, known toolkit repairs, evaluated skill patches and replay-verified guards through existing background jobs. `honey rsi autonomy on|off|status` controls the policy; status and review show automatic results and blocked reasons.

### Changed
- `reverse` skill renamed to `reverse-engineer` (`/honey:reverse-engineer`). It now vendors REA's complete official workflow and all six target guides verbatim (morluto/rea v6.7.0, skill v42) behind honey's authorization gate, so it routes and recovers exactly like REA's own skill.

### Fixed
- Proposal application no longer marks unsupported fixes or uncompleted upstream ports as applied. Forgotten learning sources, stale edits, missing evaluations and failed syncs block or defer changes. Instruction revert preserves later edits; queued failures no longer starve other work.

## [0.9.1] - 2026-10-18

### Changed
- The `qa` skill now routes to `references/e2e.md` from its exploration phase, not only the fix loop: reach controls by role→name→test id and confirm a locator is unique while exploring, and treat explore-then-pin-with-a-repro as the heart of e2e. e2e discipline now guides how `qa` drives the app, not just how it writes a regression test afterward.

## [0.9.0] - 2026-10-18

### Added
- **Animation guidance** in the `frontend` skill (`references/animation.md`, distilled from Emil Kowalski's design-engineering skills, MIT): animate by action frequency, `ease-out`/never `ease-in` for UI, sub-300ms durations, `transform`/`opacity`-only performance rules, entry/popover/stagger/spring patterns, and a review checklist. The inline motion rule now carries the headline law.
- **End-to-end test guidance** in the `qa` skill (`references/e2e.md`, distilled from the e2e framework docs): split steps into deterministic (`expect`) vs agent-driven, select by role→name→test id, verify locators before asserting, cache only verified steps, explore→pin as regression tests with a repro per finding, and secret/flaky/CI handling.

## [0.8.0] - 2026-10-18

### Added
- **Quality evals** (`evals/quality-*`, run with `--ablation with-without`) that grade skill *output* against a no-plugin baseline, not just whether a skill fires. A pressure case proved real uplift: under "just confirm it's fine, I'm in a hurry" on refund logic, honey-stack holds the line (1.0) where the base model signs off from a read alone (0.0).
- **Hard skill-listing budget gate** (`tests/test_budget.py`, enforced in CI): fails if model-invoked descriptions exceed the total budget or any skill's description would be truncated. Adding a skill can no longer silently break the listing.
- **Freshness tracking**: every skill carries a `reviewed:` date; `tests/test_freshness.py` requires it and `honey doctor` warns when a skill's version-specific facts are over a year old.
- **One-line installer** (`install.sh`): `curl … | bash` clones/updates the repo and shows a dry run before you approve the apply.
- **`honey install --skill-budget <fraction>`**: reversibly raises Claude's `skillListingBudgetFraction` for more skill-description room alongside other installed skills.
- **Router disambiguation**: `honey` now spells out which skill wins when two overlap (debug vs incident, perf vs database, tdd vs test-design vs qa, review vs verify, ship vs deploy, security vs upgrade, docs vs unslop).

### Changed
- Strengthened the always-on proof law: never sign off that a change is fine or safe to ship from a read alone, and hold that on money/auth/data paths even under time pressure.
- Tightened several model-invoked skill descriptions to stay within the listing budget.

### Known
- On strong base models, the other quality cases (debug plan, edge-case listing, API error contract, done-discipline) score 1.0 both with and without honey — these are consistency/preference skills, not capability uplift there. The evals stay as a regression floor.

## [0.7.0] - 2026-10-18

### Added
Six production-engineering skills, each with current (2026) reference files, decision rules, and confirmation gates for risky actions:
- **`database`** — schema design, safe/online (expand-contract) migrations, indexing, slow-query and N+1 fixes, transactions and isolation levels. Postgres-first. Confirms before any production migration.
- **`perf`** — profile-first performance work (per-stack profilers, USE/RED), hot-path fixes, caching layers and invalidation, and load testing with p50/p95/p99. Confirms before load-testing anything live.
- **`observability`** — structured logging, metrics (RED/USE, cardinality traps), distributed tracing, error tracking, SLOs and burn-rate alerting, OpenTelemetry-first.
- **`api-design`** — REST/GraphQL/gRPC/tRPC choice, resource modeling, RFC 9457 error contracts, cursor pagination, idempotency keys, versioning without breaking, schema-first OpenAPI/proto/GraphQL.
- **`upgrade`** — safe dependency upgrades (one major per PR, gated by tests), CVE triage with reachability (osv-scanner, govulncheck), forcing patched transitive deps, framework codemods, runtime EOL, lockfile hygiene.
- **`incident`** — production outage response: severity, mitigate-first (rollback/flag/scale), safe live investigation with the four golden signals, communication, and a blameless postmortem. The agent recommends but never executes a prod mitigation without human approval.

## [0.6.0] - 2026-10-18

### Added
- **`deploy` skill** — platform-aware deploys and infrastructure: AWS (ECS/Lambda), GCP Cloud Run, Azure Container Apps, Vercel/Netlify/Fly/Railway/Render/Cloudflare, plus Docker, Kubernetes/Helm, Terraform/OpenTofu/Pulumi/CDK, and GitHub Actions OIDC CI/CD. Reference files per platform with the top real errors and fixes, a production/destructive-op confirmation gate, and a failed-deploy diagnosis loop.
- **`test-design` skill** — derives the full test matrix and hard edge cases for a feature (equivalence/boundary/decision-table/state/pairwise, data-type and systemic edge-case checklists, property-based tests, mutation-testing as the real coverage signal, level/double choices), then hands concrete cases to `tdd`.
- **`tooling` skill** — monorepo (Turborepo/Nx/pnpm workspaces + Changesets), micro-frontends (Module Federation 2), microservices + Pact contracts, pre-commit hooks (Husky v9/Lefthook/pre-commit), lint/format/typecheck (Biome 2, ESLint 10 flat config, Ruff), test wiring (Vitest 3) and a GitHub Actions pipeline, each with current configs and honest "is this overkill?" guidance.

## [0.5.0] - 2026-10-18

### Added
- `reverse` skill (`/honey:reverse`, `$reverse`): drives the [REA](https://github.com/morluto/rea) toolchain to reverse-engineer a shipped target (binary, APK, JS/Electron, .NET, website, firmware, network capture) without its source, with an authorized-use gate and evidence discipline. REA is a separate MCP server the skill installs on approval; honey-stack doesn't bundle it.

## [0.4.0] - 2026-10-18

### Added
- `unslop` skill (`/honey:unslop` in Claude, `$unslop` in Codex), adapted from pstack. It removes AI tells and filler from any text you pass it: messages, posts, prompts or docs. The `docs` skill now uses it as the single source of its writing rules.

## [0.3.1] - 2026-10-18

### Added
- `honey update`: pulls the latest release into your checkout and refreshes Codex's copy.

## [0.3.0] - 2026-10-18

### Added
- **Reply indicator.** Every reply starts with `🍯 HONEY`, plus any non-default mode (e.g. `🍯 HONEY · ultra · strict`), so you can see at a glance that honey-stack is on. `honey off` hides it.

### Fixed
- The `review` skill now fires reliably on short pasted diffs: the bootstrap routes "review code/diff/PR" requests to the skill instead of an inline opinion (trigger eval 3/3, up from 2/3).
- Verified the Codex agent role format against Codex 0.163: all 4 `honey-*` agents are discovered.

## [0.2.0] - 2026-10-17

### Added
- `honey --version`.
- Release tooling: `scripts/release.py` (bumps every version field, rolls the changelog, tests, tags), CI on macOS and Linux with Python 3.9 and 3.12, and GitHub releases published automatically from tags.
- Install straight from GitHub on both agents: `claude plugin marketplace add umararshad4/honey-stack` and `codex plugin marketplace add umararshad4/honey-stack`.

### Changed
- `honey install` now leaves your `CLAUDE.md` / `AGENTS.md` and your other skills alone by default. Overwriting the instruction files (`--slim-instructions`) and retiring absorbed MCT / Matt Pocock duplicates (`--retire-legacy`) are opt-in.
- Rewrote the README as a public quick-start.

## [0.1.0] - 2026-10-17

### Added
- An always-on bootstrap for Claude Code and Codex: task sizing, the smallest-complete-change ladder, proof before done, decisions over questions. Three mode axes (`honey ultra|lite|full`, `strict|light`, `terse|normal`) plus `honey off`.
- 22 skills distilled from Superpowers, Ponytail, Matt Pocock's skills, pstack and gstack, plus 4 subagents (also rendered for Codex).
- Safety hooks: a destructive-command guard, a scan for secrets in newly written text, and a receipt gate that only fires after turns that edited files.
- The RSI engine:
  - an observer with harness-origin filtering and an event log shared by both agents
  - `remember:` notes
  - a detached reflector with budget caps
  - a deterministic curator: promotion after 2 sessions, decay, budgets
  - `/rsi review` (Tier 1 instruction edits, Tier 2 eval-gated skill patches on branches, Tier 3 learned guards)
  - `/rsi coach`, `/rsi bootstrap`, the upstream radar, a golden suite and holdout sessions
- `honey install | uninstall | doctor | sync`: dry-run first, recorded in a manifest, fully reversible.

[Unreleased]: https://github.com/umararshad4/honey-stack/compare/v0.18.3...HEAD
[0.18.3]: https://github.com/umararshad4/honey-stack/compare/v0.18.2...v0.18.3
[0.18.2]: https://github.com/umararshad4/honey-stack/compare/v0.18.1...v0.18.2
[0.18.1]: https://github.com/umararshad4/honey-stack/compare/v0.18.0...v0.18.1
[0.18.0]: https://github.com/umararshad4/honey-stack/compare/v0.17.2...v0.18.0
[0.17.2]: https://github.com/umararshad4/honey-stack/compare/v0.17.1...v0.17.2
[0.17.1]: https://github.com/umararshad4/honey-stack/compare/v0.17.0...v0.17.1
[0.17.0]: https://github.com/umararshad4/honey-stack/compare/v0.16.4...v0.17.0
[0.16.4]: https://github.com/umararshad4/honey-stack/compare/v0.16.3...v0.16.4
[0.16.3]: https://github.com/umararshad4/honey-stack/compare/v0.16.2...v0.16.3
[0.16.2]: https://github.com/umararshad4/honey-stack/compare/v0.16.1...v0.16.2
[0.16.1]: https://github.com/umararshad4/honey-stack/compare/v0.16.0...v0.16.1
[0.16.0]: https://github.com/umararshad4/honey-stack/compare/v0.15.0...v0.16.0
[0.15.0]: https://github.com/umararshad4/honey-stack/compare/v0.14.0...v0.15.0
[0.14.0]: https://github.com/umararshad4/honey-stack/compare/v0.13.0...v0.14.0
[0.13.0]: https://github.com/umararshad4/honey-stack/compare/v0.12.0...v0.13.0
[0.12.0]: https://github.com/umararshad4/honey-stack/compare/v0.11.0...v0.12.0
[0.11.0]: https://github.com/umararshad4/honey-stack/compare/v0.10.0...v0.11.0
[0.10.0]: https://github.com/umararshad4/honey-stack/compare/v0.9.1...v0.10.0
[0.9.1]: https://github.com/umararshad4/honey-stack/compare/v0.9.0...v0.9.1
[0.9.0]: https://github.com/umararshad4/honey-stack/compare/v0.8.0...v0.9.0
[0.8.0]: https://github.com/umararshad4/honey-stack/compare/v0.7.0...v0.8.0
[0.7.0]: https://github.com/umararshad4/honey-stack/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/umararshad4/honey-stack/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/umararshad4/honey-stack/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/umararshad4/honey-stack/compare/v0.3.1...v0.4.0
[0.3.1]: https://github.com/umararshad4/honey-stack/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/umararshad4/honey-stack/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/umararshad4/honey-stack/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/umararshad4/honey-stack/releases/tag/v0.1.0
