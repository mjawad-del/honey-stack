# Private session capture and recovery

**Goal:** Enable automatic capture for the current workspace, implement Codex conversation recording, and prove cloud synchronization and recovery on another machine.
**Approach:** HOLD the requested scope. Reuse the installed Delta CLI contract 8, private producer-bound threads, and the official Claude project policy. Add a per-user transcript observer for opted-in Codex workspaces: it does not depend on native hook trust. Keep immutable pending records until individually acknowledged. Visible conversation messages only; no reasoning, tool payloads, repository attachments or historical backfill.
**Design:** Recorded here from the explicit request. Private visibility and future-only capture are implementation defaults announced to the user.

## What already exists
- `honey/delta.py`: CLI, project policy, authenticated lookup and cloud status.
- `honey/delta_handoff.py`: local Git/evidence provenance and restore checks.
- Delta CLI: private asynchronous upload and idempotent producer/sequence protocol.
- Native Codex JSONL: real session metadata and visible messages. Native Honey hooks are presently unverified, so hook-only capture would not meet the request.

## Global constraints
- Scope opt-in by canonical project path, persisted outside Git. No whole-machine transcript upload.
- Capture only root Codex sessions with matching initial session identity/project and post-activation timestamps. Ignore inherited subagent and system/developer content.
- Delta receives finalized visible user/assistant text with stable message IDs. No remote agent turn is started.
- Persist before sending; preserve record bytes and sequence across timeout/crash; validate receipts and remote producer sequence. No blind mutation retries.
- Surface configuration, service activity, local acknowledgement, upload state and recovery evidence separately.
- Stop new Honey submissions through an explicit off command. Already accepted Delta records may finish uploading. Never change native hook trust.
- No production data or credentials in committed tests/reports. Cloud tests use disposable synthetic content first.

## File map
- `honey/delta_capture.py`: transcript validation, translation and durable capture outbox (create).
- `honey/delta_service.py`: project opt-in registry, observer, service lifecycle and status (create).
- `honey/delta.py`, `honey/cli.py`: capture command routing and existing status/setup integration (modify).
- `tests/test_delta_capture.py`, `tests/test_delta_service.py`: focused regression proof (create).
- `docs/delta.md`, `skills/delta/SKILL.md`, `CHANGELOG.md`: supported behavior and operational instructions (modify).

## Review focus / failure modes
| Failure | Required behavior / test |
|---|---|
| Inherited/subagent metadata or different project | No upload; initial root identity and path validated |
| Partial JSONL write / old history | Defer incomplete line; exclude pre-activation records |
| Lost or partial Delta receipt | Retry identical pending records; never reuse sequences for new content |
| Offline / auth failure / incompatible contract | Retain pending text privately, expose named failure, back off |
| Observer restart / concurrent observer | State lock prevents competing producers and duplicates |
| Opt-out or stale installation | Stop new capture; service points to stable launcher; visible status |

## Parallel lanes
| Task | Files | Dependencies |
|---|---|---|
| 1. Transcript and outbox | delta_capture.py, test_delta_capture.py | Contract spike |
| 2. Activation and observer | delta_service.py, test_delta_service.py, CLI and docs | Task 1 public interface; can implement shell independently |

### Task 1: Reliable transcript recording
- [x] Add a failing public capture-path test and implement validated Codex transcript capture.
- [x] Cover future-only messages, root identity, duplicate prevention, interrupted reads, partial acknowledgements, restart and API failures.
- [x] Verify `python3 tests/test_delta_capture.py` (26 tests on Python 3.9 and 3.14).

### Task 2: Automatic project capture
- [x] Add opt-in/off/status and observer commands with a macOS per-user LaunchAgent using the stable installed Honey launcher.
- [x] Preserve the official private Claude policy for Git projects; support Codex opt-in for this non-Git scratch workspace.
- [x] Verify `python3 tests/test_delta_service.py` (13 tests) and existing Delta tests (31).

### Task 3: Live proof
- [x] Synthetic native capture and retry proof; server acknowledgements, private upload and exact readback all passed.
- [x] Enabled private capture for this workspace; the real post-activation assistant message was recorded and read back. Release-owned LaunchAgent installation remains in delivery.
- [ ] Read back on a separate machine and verify exact content. Blocked: this session exposes only the Mac and SSH has no second-machine target. User asked to proceed without supplying one; no machine was invented or provisioned.

### Task 4: Delivery
- [ ] Review full diff; run complete Python suite, release consistency and diff checks.
- [ ] Commit, push, PR, merge after required checks, verify published release and both installed caches.
- [ ] Verify observer uses released implementation and retains functioning capture.

## Not in scope
- Organization sharing or invitations: private capture requested and sufficient.
- Importing all historical sessions: not required for automatic future capture.
- Collaborative remote control or file synchronization: conversation capture and existing handoff provenance remain the supported scope.
