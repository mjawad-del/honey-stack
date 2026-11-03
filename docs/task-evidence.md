# Task scope and evidence

How honey records what a task may touch and which checks prove it. Back to the [README](../README.md).

Hooks retain a bounded task record: inspection or execution, workspace, targets and required checks. “Continue” and status questions retain the task. Task classification is advisory and does not block edits or grant permission. Follow the user's scope and host permissions; natural-language classification and command recognition are heuristics.

```bash
honey task status --json
honey task set --mode execute --operation code --cwd /actual/project --check tests --check lint
honey task verify --check tests --tool-call <actual-test-call-id>
honey task verify --check lint --tool-call <actual-lint-call-id>
```

Replace paths and call IDs with the actual values. Codex uses `CODEX_THREAD_ID`; otherwise add `--agent claude --session <session-id>` to each command. Bind the project before running its checks. `task set` starts a new bounded contract; it cannot promote an existing inspection task to execution without a user request.

The default code check recognizes direct test/lint commands. Explicit checks link a fresh successful result; the agent must still check that the command proves the named requirement. Later edits, potentially mutating shell commands, failed reruns and incomplete results invalidate proof. When start events are delivered, checks started before a mutation or in a previous task cannot verify the new state. Completion claims without evidence trigger one reminder per evidence cycle; honest unresolved reports and inspection answers are allowed.

For `--operation uninstall`, required checks are `application`, `helpers`, `startup` and `preserved-data`. Declare concrete targets with `--target`; `task verify --check application --absent /Applications/Example.app` measures and rechecks absence. `--present` checks existence. Helpers and startup require checks of their actual registrations, and preserved data requires an explicit check of what should remain. `--operation startup` requires its resulting registration to be checked.

Automatic classification distinguishes removing an application from editing its interface. “Remove the app logo” or “remove this button completely” uses ordinary verification; “uninstall VLC” or “remove Example.app” uses uninstall checks. Ambiguous targets still need an explicit task operation; classification does not establish permission to delete them.

Known shell/read/edit payloads, including `exec_command` with `cmd`, are normalized. JavaScript wrappers remain opaque unless the host delivers supported inner events. Missing hooks, unobserved external edits and concurrent last-writer-wins session updates limit the evidence; `doctor` reports delivery and coverage without claiming runtime enforcement.
