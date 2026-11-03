# Session audit fixes plan

**Goal:** remove false uninstall checks and stale RSI suggestion work, then ship and install the release.
**Approach:** HOLD. Reuse task classification, publication and proposal state rather than add another workflow. Keep remote issue closure separate from verified application.
**Design:** audit/HONEY-REPORT.md in the originating session (private local evidence; not published).

## Global constraints
- No changes to user capture policies, no hook-trust bypass, no arbitrary repair execution.
- Preserve user decisions and private evidence. GitHub reads and writes use gh.
- API errors must not manufacture closure or application; verified reopening can re-enable automatically suspended work.

## What already exists
Task advance assigns operation-specific verification. Publication persists exact issue URLs. Autonomous jobs already validate repairs and evaluations.

## File map and parallel lanes
- `honey/tasks.py`, `tests/test_task_evidence.py`: UI versus app removal classification (main thread).
- `honey/rsi/` publication, improvement, review and status paths plus their focused tests: issue-state reconciliation (independent worker).
- `docs/rsi.md`, `docs/task-evidence.md`, `CHANGELOG.md`: document final semantics (main thread after implementation).

## Review focus
- UI component names containing app and requests ending completely.
- Real uninstalls retain their four checks; explicit CLI operation remains authoritative.
- Closed/reopened issue identity, malformed/error responses, and previously manual decisions.
- Reconciliation occurs before preparation/application and cannot recreate closed suggestions.

## Tasks
- [x] Reproduce UI removal misclassification through UserPromptSubmit and assert normal verification instead of uninstall checks; preserve real app uninstalls. Verify: `python3 tests/test_task_evidence.py`.
- [x] Reproduce stale proposals; reconcile validated remote state before automated work, retain closure reason and allow verified reopening. Verify focused publication/autonomy/improvement tests.
- [ ] Review combined diff, run all test scripts, release consistency and diff checks; push PR, wait for required CI, merge and verify release plus both installed copies.

## Failure handling
GitHub unavailable or malformed: expose reconciliation failure and preserve recorded state, never report applied. Unknown task target: use ordinary verification; explicit uninstall operation remains available.

## Not in scope
Automatic conversation uploads require project policy choices. General model-generated runtime repair and new event adapters need separate verified designs; this release does not fabricate autonomous delivery success. Native hook approval remains a host review action and cannot be repaired by changing repository trust hashes.

## Validation record
Classification and issue reconciliation regressions failed before their fixes and passed after. Independent review found and corrected child-drafting starvation and scorecard regeneration for closed automatically applied parents. A disposable copy of the local queue reconciled 15 closed and 9 pending issues against GitHub with zero errors. Native hook trust and project capture policy remain explicit host/user actions.
