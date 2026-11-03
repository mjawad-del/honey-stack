---
name: delta
description: "Recall Delta history, configure capture, or attach it to a handoff."
disable-model-invocation: true
license: MIT
metadata:
  reviewed: "2026-11-02"
  sources: "honey-stack original; Delta CLI 0.23.0 official guides: https://delta.dev/docs/cli/get-started and https://delta.dev/docs/cli/claude-code"
---

# Delta history

Use existing Delta history as optional context on Claude Code and Codex. The separately installed official Delta plugin owns Claude capture; Honey's opted-in transcript observer records visible Codex conversation text. History commands only read; `setup --apply` changes Claude capture policy, `capture enable` activates private Codex capture, and `attach` updates a local handoff.

Run commands with this skill's bundled runtime: `python3 "<plugin root>/bin/honey" delta ...`. The plugin root is two directories above this skill's directory. Examples below abbreviate that prefix as `honey`.

Commands return JSON. A nonzero exit reports invalid input or failed retrieval; an explicit `ready: false` is a supported readiness result.

## 1. Check availability

Run `honey delta status` once when readiness is unknown in this session. Reuse that result for subsequent lookups; a lookup error is a reason to check again.

- `ready: true`: the CLI is installed and authenticated. `readiness.history_readable`, `capture.configured`/`enabled`, and `readiness.cloud_uploaded` are separate facts; `null` means unverified.
- `not_installed`: explain that Delta is optional and point to https://delta.dev/cli. Continue a normal local handoff.
- `signed_out` or `signing_in`: explain that `delta auth login` completes sign-in. Do not start login or change sync policy without a setup request.
- `unsupported_version`: Delta 0.22.0 or newer is required. An upgrade is separate from reading history.
- Nonzero exit or `error`: report the error. Failed retrieval is not an empty history.

For a known session binding, use `honey delta status --thread <reference> --source <source> --external-id <session-id>` to check the actual local read and upload state. Reuse the source/session pair returned by the recording host; a title match does not identify the current session.

Done when: the requested capability or its specific blocker is known; authentication alone is not reported as cloud readiness.

## 2. Search or read

| Request | Command |
|---|---|
| Recover a previous decision | `honey delta recall "<terms>"` |
| List matching conversations | `honey delta search "<terms>"` |
| Find work touching a path | `honey delta search "<terms>" --edited-path "src/**"` |
| Search only user messages | `honey delta search "<terms>" --scope user` |
| Check a summary's freshness | `honey delta summary <reference>` |
| Inspect a supplied thread | `honey delta read <thread-id-or-link>` |
| Read another bounded window | `honey delta read <thread-id-or-link> --messages 2-10` |

Search defaults to five matches; `--limit` accepts 1–20. Prefer specific terms, an edited-path filter and the appropriate scope (`user`, `messages`, `visible`, `metadata`, or `auto`). Every keyword group must match; `|` separates alternatives. An empty `threads` list means no match. `recall` returns candidates when ambiguous: refine the terms or select an exact `--thread` instead of guessing.

`recall` locates the strongest latest literal keyword match and returns it with adjacent messages. This is retrieval, not a semantic verdict: inspect the excerpt before answering. `summary` withholds missing or stale summary text. `read` defaults to message `1-1`; request the relevant window, at most twenty messages. Accept exact opaque IDs or HTTPS `delta.dev/join/...` links.

Outputs default to 12,000 characters; `--max-chars` accepts 2,000–64,000. `truncated: true` means content was shortened; narrow the range or raise the explicit budget before relying on an omitted detail. Preserve source IDs and message numbers when citing. Structured errors retain safe error codes and retry hints; only explicitly retryable failures receive one bounded retry.

Done when: the answer names the source and message window, or reports no match/inaccessible history without inventing context.

## 3. Handoff reference

The `handoff` skill owns the human summary. After it creates the file, attach the supplied or authoritatively identified thread and relevant window:

```sh
honey delta attach /absolute/handoff.md <reference> --messages 31-35 --evidence /absolute/test-report.json
```

Add `--source <host-source> --external-id <session-id>` when the recording host provides that binding; this enables an independent upload/access check. The command preserves the human summary and records Git state, evidence hashes and historical provenance. A missing Delta installation records the reference as unavailable; the local summary remains usable. A bare local ID does not imply a share URL.

On restore, run `honey delta restore /absolute/handoff.md --project /current/repository`. Read `git` matches, each evidence status and `history.available`; changed or missing artifacts are explicit. The `evidence_kind` is historical even when hashes match. Rerun needed checks through the `verify` skill.

Without a supplied reference, write the normal local handoff. Do not guess a thread from its title or create/share/upload one to obtain a link. If the reference cannot be read, keep the local summary and label the external reference unavailable. On restore, compare with live Git state and rerun needed checks; the thread does not reproduce ignored files, dependencies, databases, or test freshness.

Done when: the human summary survives attachment/restoration, drift is known and each reference is verified or explicitly unavailable.

## 4. Capture setup

For requested Codex capture, use `honey delta capture enable --project /workspace --organization org_ID`, then inspect `honey delta capture status --project /workspace`. Enabling uploads future visible conversation text privately and installs a macOS per-user observer; it supports plain directories as well as Git workspaces. The observer uses real Codex transcripts, not simulated hooks, and does not require native hook approval. Keep the explicit project scope; never enable all projects by implication.

Verify a real post-activation message and its Delta readback before reporting working capture. Status distinguishes local acknowledgements from cloud upload. `capture disable --project /workspace` stops Honey submitting new records; Delta can still upload records already accepted locally, and uploaded history is not deleted. For other Unix service managers (not Windows), configure with `enable --no-service` and run `capture run`; never call the policy alone an active observer. Unsupported transcript formats must be reported. This path excludes instructions, reasoning, tools, images, inherited subagents and repository files. Do not imply full filesystem recovery or collaborative agent control.

For a setup request, preview the repository policy first:

```sh
honey delta setup --project /repository --mode private --organization org_ID
```

`--apply` writes that exact policy, preserving unrelated settings and excluding it from Git. `manual` disables automatic capture; `private` uploads new sessions for the account; `organization` shares them with that organization. Private sync still stores conversation and checkout data on Delta's servers. This command configures the official Claude plugin, does not start a recording or change an existing thread's audience, and reports `capture_verified: false` until actual capture is checked in a new Claude session. See [official setup](https://delta.dev/docs/cli/claude-code).

Done when: the requested policy is applied or previewed, and configured capture is distinguished from observed synchronization.
