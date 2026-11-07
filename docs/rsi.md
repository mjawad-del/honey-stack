# RSI: recursive self-improvement

How honey learns from sessions, applies improvements and publishes them. Back to the [README](../README.md).

## How it learns

1. **Observe:** hooks log bounded, redacted user requests, corrections, "still broken" and which skills fired. Feedback distinguishes behavior, requirements, preferences and environment failures. Task identity and explicit skill references guide attribution; ambiguous feedback remains uncertain instead of blaming every recent skill.
2. **Reflect:** after a session with signal, a small model (Haiku via Claude, ~$0.002; else Codex) proposes evidence-backed WHEN → THEN rules. If one installed engine fails, the other is tried. Failed calls have their own daily cap and do not use up successful reflections; a session that fails three times in a row waits for new events instead of retrying forever.
3. **Curate:** a rule goes live once 2 sessions back it; it goes stale after 30 days and is archived after 90.
4. **Apply:** review mode queues changes for `/rsi review`. Autonomous mode applies supported changes after validation:

| Tier | Changes | Who decides |
|---|---|---|
| 0 | Learned rules in each session's brief (≤1.5 KB) | Automatic |
| 1 | Instruction edits and known toolkit repairs, with backups | You, or autonomous RSI |
| 2 | Skill patches and new skills that pass regression evaluations | You, or autonomous RSI |
| 3 | Enforced guards (≤1% false positives on replay) | You, or autonomous RSI |

| Command | Use when |
|---|---|
| `/rsi autonomy on / off / status` | Enable, disable, or inspect automatic application |
| `/rsi publish on / off / status / run` | Publish pending suggestions as GitHub issues and validated skill changes as PRs |
| `/rsi improve` | Run a due improvement pass and apply eligible proposals |
| `/rsi status` | Check it's running and what's queued |
| `/rsi review` | Claude's statusline shows `rsi:N`; decide on proposals |
| `/rsi coach` | You want an evidence-based report on what's improving or repeating |
| `/rsi bootstrap` | Fresh install; seed rules from your existing agent memories |
| `/rsi why <id>` · `forget <id>` | Inspect a rule, or reject it for good |

Codex: `$rsi …` instead of `/rsi …`.

A weekly job also checks upstream toolkits and surfaces only the changes that match friction you had.

## Configuration and autonomy

Settings live in `~/.honey/config.json`. All keys are optional:

```json
{
  "mode": "direct",
  "budgets":  {"enabled": true, "reflection_usd": 0.15, "weekly_usd": 1.5, "monthly_usd": 5},
  "autonomy": {"min_sessions": 2, "holdout_rate": 0}
}
```

- `holdout_rate: 0.1` leaves the brief out of 10% of sessions, so `/rsi coach` can measure whether rules help.
- `budgets.enabled: false` removes Honey's rolling weekly/monthly dollar caps and the dollar limits passed to drafting and evaluation calls. Usage is still recorded; unknown usage stays unknown. The daily improvement cadence, reflection rate limits, validation, and provider account limits still apply. `honey rsi status --json` reports `budget_caps_enabled`.
- **Privacy:** RSI keeps its session evidence local and redacts stored snippets. Network use includes the configured model provider, radar's public `git fetch`, and opt-in GitHub publication and issue reconciliation. Published suggestions omit raw session evidence and personal preferences.

Enable autonomous application with `honey rsi autonomy on`. It persists in `~/.honey/config.json`; other installations keep review mode until enabled. Session-end catchup and the weekly job apply up to 20 pending proposals per pass. `honey rsi autonomy off` returns to manual review. `rsi off` still pauses observation and jobs for the current session.

With autonomy enabled, session-end and startup catchup jobs attempt one evidence-backed skill improvement every 24 hours. There is no background daemon: a session triggers due work. Existing validated proposals still apply each pass. Failures rotate behind other eligible work; a blocked patch does not freeze unrelated skills. Pending sync recovery pauses new drafting until the installed copy is consistent.

When publication is enabled, automatic work first checks the exact published suggestion issues against the configured GitHub repository. A verified closed issue retires its local proposal as `closed`, preserving GitHub's reason; it does not count as an implemented fix. A verified reopening restores only proposals closed by this reconciliation, never a manual skip or refusal. Closed or unreadable linked suggestions also defer their child patches. GitHub failures preserve local decisions and defer linked work until verification succeeds. `honey rsi status --json` exposes `suggestion_issues` with the recorded remote state, closure and reconciliation errors without making network calls or changing decisions.

Skill diagnoses are routed to the existing patcher using verified user quotes. Repeated requests can produce a new skill only with actual requests from at least two sessions and positive/negative trigger evaluations. Keyword-only suggestions and unsupported runtime diagnoses remain pending. Genuine user evidence is evaluated separately from external tool output; older quarantined records are recovered only against their original user events.

With dollar caps enabled, the patcher reserves the draft and both evaluations against the existing weekly/monthly budgets before starting. Per-call limits are $0.15 for drafting and $0.35 for each evaluation. Unknown usage after a timeout retains the reserved cost; reported usage replaces it. A budget deferral does not consume the daily generation attempt, so due work can retry when budget becomes available. With dollar caps disabled, drafting and evaluation omit provider dollar limits and failed calls retain unknown usage rather than a false capped estimate. Evaluator errors and incomplete results never count as successful tests. Each arm includes generated cases, existing name-matched skill cases and independently maintained behavior cases for read-only preservation, scoped edits and failed verification. Fixture hashes and exact candidate content are checked before a proposal can apply. Missing or changed fixtures require re-evaluation, including for older proposals. This evaluator supports Claude plugin eval; it does not establish Codex behavior or coverage of every other skill.

`honey task status` exposes task/session tool counts, repeated reads/checks, failures, unknown results and reported durations. `/rsi coach` aggregates them with denominators. Repetition can be necessary, missing durations are not estimated, and these observations are not measured speedups. Uncertain feedback is visible but excluded from negative skill scores and patch evidence.

When a host does not provide the inner events for an opaque tool wrapper, use `honey task run --check verification -- python3 tests/run.py` after binding the execution task to its Git project root with `honey task set --mode execute --operation code --cwd /path/to/project`. This runs the real check without a shell and records its exit status and source snapshot. Printed success cannot manufacture proof; changed source, HEAD or index makes the result stale. The fallback covers tracked and non-ignored files (up to 20,000 files/64 MiB), rejects source symlinks, and does not claim to snapshot ignored dependencies or the external environment. Normal host-event verification remains available.

Use `honey rsi status` for the latest automatic result and `honey rsi review --json` for recent changes and blocked reasons. Unsupported diagnoses remain pending; RSI never executes a proposal's free-form fix text. Diagnosis and new-skill parents complete only after their evaluated child patch is applied, as do upstream ports. Evaluation overlays the current runtime into its worktree and application checks that snapshot. Edited patch targets, runtime drift and diverged branches block application; unrelated documentation edits are preserved, and configured cost limits still apply. Run `honey sync` from the source checkout once to refresh Codex and record the editable source location; the plugin cache is never an editable source.

Autonomous mode only edits or removes lines inside honey's managed block; edits to lines you wrote yourself always wait for `/rsi review`. A `remember:` note sent while a background RSI job holds the lock is queued and saved when the job finishes. Instruction edits have conflict-checked `honey rsi revert <id>` snapshots; toolkit repairs retain installer undo records. Guard IDs remain inspectable with `honey rsi guards`. A failed skill sync records `sync_pending` for retry. Legacy instruction snapshots used random short hashes, so automatic revert refuses those; the original text remains in the proposal's `before` field for manual recovery. Automatic decisions do not count as user feedback.

## Publishing improvements

Run `honey rsi publish on` from your Honey source checkout to authorize publishing pending suggestions as issues and applied skill improvements as PRs to its GitHub origin. This pins the repository and its default branch in your local configuration and requires authenticated `gh` access with permission to create issues, push branches and create PRs. `honey rsi publish off` stops publication; `honey rsi publish status --json` shows the configuration. Older PR-only opt-ins retain their previous behavior until `publish on` is run again.

Pending toolkit diagnoses, new-skill suggestions and upstream proposals become **GitHub issues automatically during ordinary RSI processing**, even if automatic local application is off. No implementation or passing evaluation is required to record a suggestion. Each issue identifies the proposal and explicitly says it is unimplemented and needs confirmation. Only the bounded, redacted title, diagnosis and proposed next step are shared; session quotes, session identifiers and local evidence are excluded. Closed issues are respected and not recreated. Issue publication does not mark a local proposal applied. `honey rsi review` shows the issue link. `honey rsi publish run --json` publishes the current eligible backlog without invoking paid reflection or evaluation.

The full flow is **suggestion → GitHub issue → evaluation → apply and sync locally → skill-only commit → PR**. Automatic passes publish eligible applied skill patches when autonomy and publication are enabled; explicitly applying a skill patch also attempts publication. Existing eligible applied patches are included. `honey rsi status` reports published URLs and exact heads, or blocked reasons. The proposal's `github_issue` and `publication` records retain the issue URL and PR SHA/URL respectively. Failed GitHub requests retry on a later pass and look for an existing issue or PR first. Publication failures do not undo successful local application. Published means the issue or PR exists, not that a suggestion is proven or a PR has passed CI or merged.

For unattended delivery, run `honey rsi publish automatic`. This explicitly enables autonomous application, suggestion publication, CI-gated PR merging and automatic updates for this installation. The existing `publish on` mode remains review-only and `publish off` stops publication. An evaluated skill PR is merged only after all four repository CI test jobs pass, its exact head and evaluated base still match, and the PR has an approving review, even when the repository itself requires none. Missing checks, missing approval, failures, changed revisions, requested changes and conflicts remain visible blockers; no admin bypass or force push is used.

After that merge, the main workflow publishes a versioned release. Subsequent RSI passes detect a stable release containing the merge and install it. A clean local checkout that is still exactly at the known private evaluation commit is preserved in an `rsi/applied-backup-*` branch before moving to the public release history. New local commits, uncommitted work or a different active branch prevent reconciliation. Installation errors retry without merging twice. General automatic updates continue at session start with their normal hourly limit. This is event-driven processing, not a continuously polling daemon; delivery resumes on the next RSI pass.

Personal preferences, instruction files, machine repairs, guards, session logs, and generated evaluation prompts stay local. Public commits start from the remote base and contain only the evaluated `skills/<name>/SKILL.md`; local evaluation commits and their history are not pushed. PR text uses fixed metadata, never user quotes or the model's rationale. The skill text itself is shared. Runtime changes absent from the remote baseline, dirty runtime files, legacy patches without snapshots, unexpected files, and changed remote branches block publication. Automatic merges and releases require the explicit `publish automatic` opt-in; force pushes are never used. A published PR's closure is respected.
