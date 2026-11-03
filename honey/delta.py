"""Optional Delta history lookup, local capture configuration and handoff provenance."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time


JOIN_PREFIX = "https://delta.dev/join/"
ERROR_HINTS = {
    "authentication_required": "Sign in with delta auth login, then retry.",
    "wrong_account": "Use the Delta account that has access to this thread.",
    "not_visible": "This account cannot access the thread. Check its invitation or organization.",
    "organization_required": "Select the intended Delta organization.",
    "not_shared": "The thread has no verified shared copy; local history may still be available.",
    "invalid_reference": "Use the exact reference returned by Delta.",
    "invalid_invocation": "Check the requested command and message window.",
    "app_unavailable": "Delta is temporarily unavailable. Check the app and retry.",
    "incompatible_app": "Update the Delta app and CLI together.",
    "reload_required": "Resolve the thread's pending changes in Delta before retrying.",
    "session_ended": "Reconnect to the Delta thread before retrying.",
    "history_reset": "Refresh the thread reference and read its current history.",
    "watch_reset": "Reconnect to Delta before retrying.",
    "failed": "Delta could not read the requested history. Check access and the message window.",
}


def _valid_id(value):
    return isinstance(value, str) and bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{1,127}", value)) and not value.isdigit()


def _identity(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,199}", value):
        raise argparse.ArgumentTypeError("use a nonempty source or session identifier, at most 200 characters")
    return value


class DeltaError(Exception):
    def __init__(self, code, message, retryable=False, retry_after_ms=None):
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.retry_after_ms = retry_after_ms

    def payload(self):
        result = {"error": self.code, "message": str(self), "retryable": self.retryable}
        if self.retry_after_ms is not None:
            result["retry_after_ms"] = self.retry_after_ms
        return result


def _invoke(*args):
    binary = shutil.which("delta")
    if not binary:
        raise DeltaError("not_installed", "Install Delta CLI from https://delta.dev/cli to use history.")
    try:
        return subprocess.run([binary, *args], capture_output=True, text=True, timeout=20)
    except subprocess.TimeoutExpired:
        raise DeltaError("timeout", "Delta did not respond within 20 seconds. Retry after checking the Delta app.") from None
    except (OSError, UnicodeError):
        raise DeltaError("unavailable", "Delta could not be started or returned unreadable output.") from None


def _decode(result):
    try:
        data = json.loads(result.stdout)
    except ValueError:
        raise DeltaError("invalid_response", "Delta returned invalid JSON. Check the CLI installation.") from None
    if not isinstance(data, (dict, list)):
        raise DeltaError("invalid_response", "Delta returned an unexpected response.")
    return data


def _read_json(*args):
    for attempt in range(2):
        result = _invoke("--json", "thread", *args)
        try:
            data = _decode(result)
        except DeltaError:
            if not result.returncode:
                raise
            data = None
        error = data.get("error") if isinstance(data, dict) else None
        if not result.returncode and error is None:
            return data
        code = error.get("code") if isinstance(error, dict) else None
        if not isinstance(code, str) or code not in ERROR_HINTS:
            code = "command_failed"
        retryable = isinstance(error, dict) and error.get("retryable") is True
        delay = error.get("retry_after_ms") if isinstance(error, dict) else None
        delay = delay if type(delay) is int and 0 <= delay <= 60000 else None
        if attempt == 0 and retryable and (delay is None or delay <= 1000):
            time.sleep((delay or 0) / 1000)
            continue
        raise DeltaError(code, ERROR_HINTS.get(code, ERROR_HINTS["failed"]), retryable, delay)


def _limit(raw):
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if not 1 <= value <= 20:
        raise argparse.ArgumentTypeError("limit must be between 1 and 20")
    return value


def _budget(raw):
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if not 2000 <= value <= 64000:
        raise argparse.ArgumentTypeError("max-chars must be between 2000 and 64000")
    return value


def bounded(result, max_chars=12000, preserve_message=None):
    """Bound serialized context, retaining IDs and message numbers; mark every shortened result."""
    def size():
        return len(json.dumps(result, ensure_ascii=False))
    if size() <= max_chars:
        return result
    result["truncated"] = True
    if "transcript" in result:
        rows = []
        for row in result["transcript"]:
            rows.append({"number": row["number"], "role": row["role"][:40], "buffer": row["buffer"],
                         "tool_calls": [], "tool_calls_omitted": len(row.get("tool_calls", []))})
        result["transcript"] = rows
    protected = {"id", "thread", "source", "role", "messages", "status", "url", "summary_state"}
    def strings(value):
        found = []
        if isinstance(value, dict):
            for key, child in value.items():
                if isinstance(child, str) and key not in protected:
                    found.append((len(child), value, key))
                elif isinstance(child, (dict, list)):
                    found.extend(strings(child))
        elif isinstance(value, list):
            for child in value:
                found.extend(strings(child))
        return found
    while size() > max_chars:
        candidates = [item for item in strings(result) if item[0]]
        if candidates:
            length, container, key = max(candidates, key=lambda item: (
                preserve_message is None or item[1].get("number") != preserve_message, item[0]))
            container[key] = container[key][:max(0, length - max(size() - max_chars + 30, length // 4))]
        elif result.get("threads"):
            result["threads"].pop()
            result["omitted_threads"] = result.get("omitted_threads", 0) + 1
        else:
            raise DeltaError("output_limit", "Metadata exceeds the output budget. Narrow the message window.")
    return result


def search(keywords, limit=5, scope="messages", edited_path=None, max_chars=12000):
    args = ["search", "--scope", scope, "--limit", str(limit)]
    if edited_path:
        args.append("--edited-path=" + edited_path)
    data = _read_json(*args, "--", *keywords)
    if (not isinstance(data, list) or len(data) > limit
            or not all(isinstance(row, dict) and _valid_id(row.get("id")) for row in data)):
        raise DeltaError("invalid_response", "Delta returned an unexpected search result.")
    return bounded({"source": "delta", "threads": data}, max_chars)


def _reference(raw):
    if not isinstance(raw, str):
        raise argparse.ArgumentTypeError("use a Delta thread ID or HTTPS join link")
    identifier = raw[len(JOIN_PREFIX):] if raw.startswith(JOIN_PREFIX) else raw
    if not _valid_id(identifier):
        raise argparse.ArgumentTypeError("use a Delta thread ID or https://delta.dev/join/THREAD")
    return raw


def _window(raw):
    match = re.fullmatch(r"([1-9][0-9]{0,8})-([1-9][0-9]{0,8})", raw) if isinstance(raw, str) else None
    if not match or not 0 <= int(match[2]) - int(match[1]) < 20:
        raise argparse.ArgumentTypeError("messages must be FIRST-LAST with 1 to 20 messages, starting at 1 or later")
    return raw


def read_thread(reference, messages="1-1", max_chars=12000):
    data = _read_json("show", "--format", "messages", "--messages", messages, "--", reference)
    if (not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict)
            or not _valid_id(data[0].get("id")) or not isinstance(data[0].get("messages"), list)):
        raise DeltaError("invalid_response", "Delta returned an unexpected thread response.")
    rows = data[0]["messages"]
    first, last = map(int, messages.split("-"))
    if len(rows) != last - first + 1 or any(
        not isinstance(row, dict) or type(row.get("number")) is not int or row["number"] != number
        or not isinstance(row.get("role"), str) or not row["role"]
        or not isinstance(row.get("buffer"), str) or not isinstance(row.get("tool_calls"), list)
        for number, row in enumerate(rows, first)
    ):
        raise DeltaError("invalid_response", "Delta returned invalid messages or a different message window.")
    result = {"source": "delta", "thread": data[0]["id"],
              "url": reference if reference.startswith(JOIN_PREFIX) else None,
              "messages": messages, "transcript": rows}
    return bounded(result, max_chars) if max_chars is not None else result


def _projection(reference, format_name):
    data = _read_json("show", "--format", format_name, "--", reference)
    if (not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict)
            or not _valid_id(data[0].get("id")) or not isinstance(data[0].get("messages"), list)):
        raise DeltaError("invalid_response", "Delta returned an invalid transcript projection.")
    if any(not isinstance(row, dict) or type(row.get("number")) is not int or row["number"] != number
           for number, row in enumerate(data[0]["messages"], 1)):
        raise DeltaError("invalid_response", "Delta returned invalid message numbering.")
    return data[0]


def summary(reference, max_chars=12000):
    data = _projection(reference, "summarize")
    value = data.get("summary")
    fresh = data.get("summary_is_fresh") is True
    state = "missing" if not isinstance(value, str) or not value else "fresh" if fresh else "stale"
    return bounded({"source": "delta", "thread": data["id"], "title": str(data.get("name") or "")[:256],
                    "summary": value if state == "fresh" else None, "summary_state": state,
                    "message_count": len(data["messages"])}, max_chars)


def recall(keywords, reference=None, limit=5, scope="messages", edited_path=None, max_chars=12000):
    if reference is None:
        matches = search(keywords, limit, scope, edited_path)["threads"]
        if len(matches) != 1:
            rows = [{"id": item["id"], "name": str(item.get("name") or item.get("title") or "")[:256]}
                    for item in matches]
            return bounded({"source": "delta", "status": "ambiguous" if matches else "no_match", "threads": rows}, max_chars)
        reference = matches[0]["id"]
    data = _projection(reference, "full")
    if not isinstance(data.get("buffer"), str):
        raise DeltaError("invalid_response", "Delta returned no transcript buffer.")
    buffer = data["buffer"].encode("utf-8")
    def score(content):
        return sum(any(part.casefold() in content.casefold() for part in term.split("|") if part)
                   for term in keywords)
    candidates = []
    previous_end = 0
    for row in data["messages"]:
        first, last = row.get("start_byte"), row.get("end_byte")
        if (type(first) is not int or type(last) is not int or not previous_end <= first <= last <= len(buffer)
                or not isinstance(row.get("role"), str)):
            raise DeltaError("invalid_response", "Delta returned invalid transcript byte offsets.")
        previous_end = last
        try:
            content = buffer[first:last].decode("utf-8")
        except UnicodeError:
            raise DeltaError("invalid_response", "Delta message boundaries split encoded text.") from None
        if scope != "user" or row["role"] == "user":
            matched = score(content)
            if matched:
                candidates.append((matched, row["number"]))
    if not candidates:
        return {"source": "delta", "status": "no_matching_message", "thread": data["id"],
                "message_count": len(data["messages"])}
    matched, anchor = max(candidates)
    window = f"{max(1, anchor - 1)}-{min(len(data['messages']), anchor + 1)}"
    # Validate the selected window against the current thread, not just a prior projection.
    result = read_thread(reference, window, None)
    selected = next(row for row in result["transcript"] if row["number"] == anchor)
    if score(selected["buffer"]) != matched:
        raise DeltaError("history_changed", "The selected message changed during retrieval. Repeat the lookup.")
    # Reserve the longest status/claim representation before applying the final budget.
    # Updating these fields afterwards only shrinks the serialized result.
    result.update(status="partial", match={"message": anchor, "matched_groups": matched,
                                          "visible_groups": matched, "complete": False},
                  message_count=len(data["messages"]))
    # Keep the matched evidence, not a large unrelated prefix, when an excerpt is needed.
    allowance = max(256, (max_chars - 1000) // len(result["transcript"]))
    for row in result["transcript"]:
        content = row["buffer"]
        if len(content) <= allowance:
            continue
        positions = [0]
        if row["number"] == anchor:
            for term in keywords:
                for part in term.split("|"):
                    if not part:
                        continue
                    last_match = None
                    for found in re.finditer(re.escape(part), content, re.I):
                        last_match = found.start()
                    if last_match is not None:
                        positions.append(max(0, last_match - allowance // 3))
        start = max(positions, key=lambda offset: (score(content[offset:offset + allowance]), offset))
        row.update(buffer=content[start:start + allowance], excerpt_start=start,
                   excerpt_end=min(len(content), start + allowance), truncated=True)
        result["truncated"] = True
    result = bounded(result, max_chars, preserve_message=anchor)
    visible = next(row for row in result["transcript"] if row["number"] == anchor)
    result["match"]["visible_groups"] = score(visible["buffer"])
    result["match"]["complete"] = result["match"]["visible_groups"] == len(keywords)
    if result["match"]["visible_groups"] == matched:
        result["status"] = "found"
    return result


def project_root(project=None):
    directory = Path(project or os.getcwd()).resolve()
    if not directory.is_dir():
        raise DeltaError("invalid_project", "Choose an existing project directory.")
    try:
        result = subprocess.run(["git", "-C", str(directory), "rev-parse", "--show-toplevel"],
                                capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return directory
    return Path(result.stdout.strip()).resolve() if result.returncode == 0 else directory


def capture_status(project=None):
    from honey import delta_service
    codex = delta_service.project_status(project)
    config = project_root(project) / ".delta/settings.json"
    result = {"configured": False, "mode": "manual", "path": str(config),
              "enabled": False, "applies_to": "new_claude_sessions", "codex_capture": codex['enabled'],
              "codex": codex}
    if not config.exists():
        if codex['enabled']:
            result.update(configured=True, enabled=True, mode='private', path=codex['path'],
                          applies_to='codex_visible_messages_after_activation')
        return result
    try:
        if config.stat().st_size > 65536:
            raise ValueError()
        data = json.loads(config.read_text())
        policy = data.get("threads", {})
        mode = policy.get("mode", "manual")
        organization = policy.get("organization")
        if mode not in ("manual", "private", "organization") or (mode != "manual" and
                (not isinstance(organization, str) or not re.fullmatch(r"org_[A-Za-z0-9_-]{1,120}", organization))):
            raise ValueError()
        result.update(configured="mode" in policy, enabled=mode != "manual", mode=mode)
    except (OSError, ValueError, AttributeError):
        result.update(mode="invalid", error="invalid_capture_settings")
    if codex['enabled']:
        claude_enabled = result['enabled']
        result['claude'] = {key: result[key] for key in ('configured', 'enabled', 'mode', 'path')}
        result.update(configured=True, enabled=True,
                      applies_to='codex_and_configured_claude_sessions' if claude_enabled else 'codex_visible_messages_after_activation')
        if not claude_enabled:
            result.update(mode='private', path=codex['path'])
    return result


def write_private(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def setup(project, mode, organization=None, apply=False):
    root = project_root(project)
    try:
        gitdir = subprocess.run(["git", "-C", str(root), "rev-parse", "--git-dir"],
                                capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        raise DeltaError("invalid_project", "Capture setup requires a Git checkout.") from None
    if gitdir.returncode:
        raise DeltaError("invalid_project", "Capture setup requires a Git checkout.")
    if mode != "manual" and (not isinstance(organization, str) or not re.fullmatch(r"org_[A-Za-z0-9_-]{1,120}", organization)):
        raise DeltaError("organization_required", "Private or organization capture requires an explicit --organization org_ID.")
    config = root / ".delta/settings.json"
    if config.is_symlink() or config.parent.is_symlink() or config.parent.resolve().parent != root:
        raise DeltaError("unsafe_config", "Capture settings must be a regular local file.")
    tracked = subprocess.run(["git", "-C", str(root), "ls-files", "--error-unmatch", "--", ".delta/settings.json"],
                             capture_output=True, timeout=5)
    if tracked.returncode == 0:
        raise DeltaError("tracked_config", "Remove capture settings from Git tracking before changing the local policy.")
    try:
        if config.exists() and config.stat().st_size > 65536:
            raise ValueError()
        data = json.loads(config.read_text()) if config.exists() else {}
        if not isinstance(data, dict) or not isinstance(data.get("threads", {}), dict):
            raise ValueError()
    except (OSError, ValueError):
        raise DeltaError("invalid_capture_settings", "Repair the existing capture settings before applying a change.") from None
    policy = dict(data.get("threads", {}), mode=mode)
    if organization is not None:
        policy["organization"] = organization
    data["threads"] = policy
    if apply:
        ignored = subprocess.run(["git", "-C", str(root), "check-ignore", "-q", ".delta/settings.json"], timeout=5)
        if ignored.returncode not in (0, 1):
            raise DeltaError("invalid_project", "Git could not check the capture settings exclusion.")
        if ignored.returncode == 1:
            result = subprocess.run(["git", "-C", str(root), "rev-parse", "--git-path", "info/exclude"],
                                    capture_output=True, text=True, check=True, timeout=5)
            exclude = Path(result.stdout.strip())
            exclude = exclude if exclude.is_absolute() else root / exclude
            original = exclude.read_text() if exclude.exists() else ""
            write_private(exclude, original.rstrip("\n") + "\n/.delta/settings.json\n")
        write_private(config, json.dumps(data, indent=2) + "\n")
    return {"applied": apply, "path": str(config), "mode": mode, "uploads": mode != "manual",
            "audience": "organization" if mode == "organization" else "private" if mode == "private" else "unchanged",
            "applies_to": "new_claude_sessions", "capture_verified": False,
            "note": "The separate official Claude Code plugin performs capture. Private sync uploads conversations and checkout files."}


def thread_status(reference, source, external_id):
    data = _read_json("status", reference, "--source", source, "--external-id", external_id)
    if not isinstance(data, dict) or not _valid_id(data.get("thread_id")):
        raise DeltaError("invalid_response", "Delta returned invalid thread status.")
    upload = data.get("upload") or {}
    access = data.get("access") or {}
    if not isinstance(upload, dict) or not isinstance(access, dict):
        raise DeltaError("invalid_response", "Delta returned invalid upload or access status.")
    state = upload.get("state", "unknown")
    if state not in ("queued", "uploading", "uploaded", "failed", "unknown"):
        state = "unknown"
    url = data.get("url")
    if url is not None:
        try:
            if not url.startswith(JOIN_PREFIX):
                raise ValueError()
            _reference(url)
        except (AttributeError, ValueError, argparse.ArgumentTypeError):
            raise DeltaError("invalid_response", "Delta returned an invalid share URL.") from None
    return {"thread": data["thread_id"], "state": state, "access": access.get("state", "unknown"),
            "url": url, "visibility": data.get("visibility")}


def status(project=None, reference=None, source=None, external_id=None):
    capture = capture_status(project)
    result = {"ready": False, "capture": capture,
              "readiness": {"cli_available": False, "authenticated": False, "history_readable": None,
                            "capture_configured": capture["configured"], "cloud_uploaded": None},
              "cloud": {"state": "unverified", "url": None}}
    try:
        version_result = _invoke("--version")
    except DeltaError as exc:
        if exc.code == "not_installed":
            return dict(result, reason=exc.code, hint=str(exc))
        raise
    match = re.fullmatch(r"delta (\d+)\.(\d+)\.(\d+)(?:[-+][^\s]+)?", version_result.stdout.strip())
    if version_result.returncode or not match:
        raise DeltaError("invalid_response", "Expected the Delta CLI from delta.dev.")
    version = ".".join(match.groups())
    if tuple(map(int, match.groups())) < (0, 22, 0):
        return dict(result, reason="unsupported_version", version=version, hint="Update Delta to 0.22.0 or newer.")
    result.update(version=version)
    result["readiness"]["cli_available"] = True
    authentication = _invoke("--json", "auth", "status")
    data = _decode(authentication)
    state = data.get("status") if isinstance(data, dict) else None
    if state in ("signed_out", "signing_in"):
        return dict(result, reason=state, hint="Complete Delta sign-in with delta auth login.")
    account = data.get("account") if isinstance(data, dict) else None
    login = account.get("login") if isinstance(account, dict) else None
    if authentication.returncode or state != "signed_in" or not isinstance(login, str) or not login.strip():
        raise DeltaError("invalid_response", "Delta could not confirm the signed-in account.")
    result.update(ready=True, login=login)
    result["readiness"]["authenticated"] = True
    if reference:
        try:
            result["cloud"] = thread_status(reference, source, external_id)
            cloud_state = result["cloud"]["state"]
            result["readiness"]["cloud_uploaded"] = None if cloud_state == "unknown" else cloud_state == "uploaded"
        except DeltaError as exc:
            result["cloud_error"] = exc.payload()
        try:
            read_thread(reference)
            result["readiness"]["history_readable"] = True
        except DeltaError as exc:
            result["readiness"]["history_readable"] = False
            result["history_error"] = exc.payload()
    return result


def main(argv):
    if argv and argv[0] == 'capture':
        from honey import delta_service
        try:
            return delta_service.main(argv[1:])
        except DeltaError as exc:
            print(json.dumps(exc.payload()))
            return 1
        except (OSError, subprocess.SubprocessError):
            print(json.dumps({'error': 'capture_service_failed', 'message': 'The local capture operation failed; inspect capture status and service logs.'}))
            return 1
    parser = argparse.ArgumentParser(prog="honey delta", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser('capture', help='Enable, inspect or stop automatic private Codex capture')
    health = commands.add_parser("status", help="Check CLI, account, capture policy and optional thread readiness")
    health.add_argument("--project")
    health.add_argument("--thread", type=_reference)
    health.add_argument("--source", type=_identity)
    health.add_argument("--external-id", type=_identity)
    lookup = commands.add_parser("search", help="Search existing history without model queries")
    lookup.add_argument("keywords", nargs="+")
    lookup.add_argument("--limit", type=_limit, default=5)
    lookup.add_argument("--scope", choices=("messages", "user", "visible", "metadata", "auto"), default="messages")
    lookup.add_argument("--edited-path")
    lookup.add_argument("--max-chars", type=_budget, default=12000)
    read = commands.add_parser("read", help="Read a bounded window from an existing thread")
    read.add_argument("reference", type=_reference)
    read.add_argument("--messages", type=_window, default="1-1", help="Inclusive window, at most 20 messages (default: 1-1)")
    read.add_argument("--max-chars", type=_budget, default=12000)
    brief = commands.add_parser("summary", help="Read fresh summary metadata without forwarding the full transcript")
    brief.add_argument("reference", type=_reference)
    brief.add_argument("--max-chars", type=_budget, default=12000)
    remember = commands.add_parser("recall", help="Locate the strongest latest keyword match and return its message window")
    remember.add_argument("keywords", nargs="+")
    remember.add_argument("--thread", type=_reference)
    remember.add_argument("--limit", type=_limit, default=5)
    remember.add_argument("--scope", choices=("messages", "user", "visible", "metadata", "auto"), default="messages")
    remember.add_argument("--edited-path")
    remember.add_argument("--max-chars", type=_budget, default=12000)
    configure = commands.add_parser("setup", help="Preview or explicitly apply the official Claude capture policy")
    configure.add_argument("--project")
    configure.add_argument("--mode", required=True, choices=("manual", "private", "organization"))
    configure.add_argument("--organization")
    configure.add_argument("--apply", action="store_true")
    link = commands.add_parser("attach", help="Attach verified Delta and Git provenance to an existing handoff")
    link.add_argument("file")
    link.add_argument("reference", type=_reference)
    link.add_argument("--project")
    link.add_argument("--source", type=_identity)
    link.add_argument("--external-id", type=_identity)
    link.add_argument("--messages", type=_window, default="1-1")
    link.add_argument("--evidence", action="append", default=[])
    resume = commands.add_parser("restore", help="Check a handoff against current Git, evidence and Delta history")
    resume.add_argument("file")
    resume.add_argument("--project")
    resume.add_argument("--max-chars", type=_budget, default=12000)
    args = parser.parse_args(argv or ["status"])
    if args.command == "status" and any((args.thread, args.source, args.external_id)) and not all((args.thread, args.source, args.external_id)):
        parser.error("thread status requires --thread, --source and --external-id together")
    if args.command == "attach" and bool(args.source) != bool(args.external_id):
        parser.error("provide --source and --external-id together")
    if args.command in ("search", "recall") and not all(word.strip() for word in args.keywords):
        parser.error("search keywords must not be blank")
    try:
        if args.command == "status":
            result = status(args.project, args.thread, args.source, args.external_id)
        elif args.command == "search":
            result = search(args.keywords, args.limit, args.scope, args.edited_path, args.max_chars)
        elif args.command == "recall":
            result = recall(args.keywords, args.thread, args.limit, args.scope, args.edited_path, args.max_chars)
        elif args.command == "summary":
            result = summary(args.reference, args.max_chars)
        elif args.command == "setup":
            result = setup(args.project, args.mode, args.organization, args.apply)
        elif args.command in ("attach", "restore"):
            from honey import delta_handoff
            result = (delta_handoff.attach(args.file, args.reference, args.project, args.source, args.external_id,
                                            args.messages, args.evidence) if args.command == "attach"
                      else delta_handoff.restore(args.file, args.project, args.max_chars))
        else:
            result = read_thread(args.reference, args.messages, args.max_chars)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except DeltaError as exc:
        print(json.dumps(exc.payload()))
        return 1
    except (OSError, subprocess.SubprocessError):
        print(json.dumps({"error": "local_operation_failed", "message": "The local Delta operation failed; check paths and Git availability."}))
        return 1
