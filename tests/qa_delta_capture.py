#!/usr/bin/env python3
"""Explicit live capture QA using only a private, projectless synthetic conversation.

Run with --live --output /absolute/new-report-directory. Requires a signed-in
Delta CLI. Creates one synthetic thread and archives it afterwards; archiving
is not server-side deletion. Never included in tests/run.py.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch
import uuid


ROOT = Path(__file__).resolve().parents[1]


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def timestamp(moment):
    return moment.isoformat(timespec="microseconds").replace("+00:00", "Z")


def message(identifier, role, text, moment, analysis=False):
    payload = {
        "type": "message", "id": identifier, "role": role,
        "content": [{"type": "input_text" if role == "user" else "output_text", "text": text}],
        "internal_chat_message_metadata_passthrough": {
            "create_time": moment.timestamp(),
            "content_item_kinds": ["user.text"] if role == "user" else ["unknown"],
            "content_item_metadata": [{"provenance": {"type": "user"}}] if role == "user" else [],
        },
    }
    if role == "assistant":
        payload.update(status="completed", phase="analysis" if analysis else "final_answer")
        if analysis:
            payload["channel"] = "analysis"
    return {"timestamp": timestamp(moment), "type": "response_item", "payload": payload}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Create and archive a private synthetic Delta thread")
    parser.add_argument("--output", type=Path, required=True, help="New or empty directory for report and synthetic evidence")
    parser.add_argument("--organization", help="Delta organization ID; defaults to the currently selected organization")
    args = parser.parse_args(argv)
    if not args.live:
        parser.error("This probe requires explicit --live; it uploads only synthetic conversation text")
    executable = shutil.which("delta")
    if not executable:
        parser.error("Install Delta CLI and sign in before live verification")
    output = args.output.expanduser().resolve()
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        parser.error("Use a new or empty output directory; existing evidence is never overwritten")
    output.mkdir(parents=True, mode=0o700, exist_ok=True)

    report = {"live": True, "status": "failed", "started_at": timestamp(datetime.now(timezone.utc)),
              "scope": "Native Delta upload and local readback of synthetic Codex capture; not a second-machine test",
              "thread": None, "source": "honey-codex", "archived": False, "checks": [], "commands": []}
    append_calls = []
    real_run = subprocess.run

    def observe(command, *positional, **kwargs):
        # Execute the real process unchanged. The copy is evidence, never a fake response.
        entry = {"args": list(command), "input": kwargs.get("input")}
        report["commands"].append(entry)
        try:
            result = real_run(command, *positional, **kwargs)
        except subprocess.TimeoutExpired as exc:
            entry.update(error="timeout", stdout=str(exc.stdout or ""), stderr=str(exc.stderr or ""))
            raise
        entry.update(exit_code=result.returncode, stdout=result.stdout, stderr=result.stderr)
        words = list(command)
        if "thread" in words:
            index = words.index("thread") + 1
            operation = words[index] if index < len(words) else None
            if operation == "create" and result.returncode == 0:
                created = json.loads(result.stdout)
                require(report["thread"] in (None, created.get("thread_id")), "Capture created more than one synthetic thread")
                report["thread"] = created["thread_id"]
                report["external_id"] = words[words.index("--external-id") + 1]
            if operation == "append":
                append_calls.append({"args": words, "input": kwargs.get("input"), "exit_code": result.returncode})
        return result

    def native(*words, body=None, timeout=60):
        result = observe([executable, "--json", *words], input=body, text=True,
                         capture_output=True, timeout=timeout)
        require(result.returncode == 0, "Native Delta command failed: " + " ".join(words[:2]))
        return result.stdout

    def status():
        return json.loads(native("thread", "status", report["thread"], "--source", report["source"],
                                 "--external-id", report["external_id"]))

    def verify_binding(value):
        require(value.get("thread_id") == report["thread"], "Delta returned a different thread")
        producer = value.get("producer") or {}
        require(producer.get("source") == report["source"] and producer.get("external_id") == report["external_id"],
                "Delta returned a different producer binding")
        worktrees = value.get("worktrees") or {}
        require(worktrees.get("status") == "available" and worktrees.get("items") == [],
                "Synthetic capture must remain projectless")

    def check(name):
        report["checks"].append({"name": name, "passed": True})
        print("PASS " + name, flush=True)

    try:
        organization = args.organization or json.loads(native("org", "current"))["organization"]["id"]
        require(isinstance(organization, str) and organization.startswith("org_"), "Invalid Delta organization")
        report["organization"] = organization
        with tempfile.TemporaryDirectory(prefix="honey-delta-capture-qa-") as temporary:
            directory = Path(temporary)
            project = directory / "project"
            project.mkdir()
            codex_home = directory / "codex"
            sessions = codex_home / "sessions"
            sessions.mkdir(parents=True)
            state_dir = directory / "states"
            native_id = str(uuid.uuid4())
            moment = datetime.now(timezone.utc)
            since = timestamp(moment - timedelta(seconds=1))
            marker = "honey-native-capture-qa-" + native_id
            expected = [("user", marker + ": retain the synthetic decision."),
                        ("assistant", marker + ": the final synthetic choice is blue."),
                        ("user", marker + ": later appended message — café 日本語.")]
            rows = [{"timestamp": timestamp(moment), "type": "session_meta", "payload": {
                "id": native_id, "session_id": native_id, "source": "vscode", "cwd": str(project),
            }}, message("qa-request", *expected[0], moment),
                message("qa-excluded-analysis", "assistant", "Synthetic excluded analysis sentinel.", moment, analysis=True),
                message("qa-answer", *expected[1], moment)]
            transcript = sessions / ("rollout-" + moment.strftime("%Y-%m-%dT%H-%M-%S") + "-" + native_id + ".jsonl")
            transcript.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
            report.update(native_session_id=native_id, since=since, expected_messages=expected)
            environment = {"HONEY_HOME": str(directory / "honey"), "CODEX_HOME": str(codex_home),
                           "HONEY_NO_AUTOUPDATE": "1", "PYTHONDONTWRITEBYTECODE": "1"}
            with patch.dict(os.environ, environment):
                sys.path.insert(0, str(ROOT))
                from honey import delta_capture

                def capture(label):
                    with patch.object(delta_capture.subprocess, "run", side_effect=observe):
                        result = delta_capture.capture_file(transcript, str(project), since, organization, state_dir)
                    report[label] = result
                    require(result.get("status") in ("pending", "locally_recorded") and result.get("pending") == 0
                            and result.get("error") is None,
                            label + " failed: " + json.dumps(result))
                    require(result.get("thread") == report["thread"], "Capture returned a different thread")
                    return result

                first = capture("initial_capture")
                require(first.get("captured") == 2 and first.get("acknowledged") == 2, "Initial capture did not acknowledge exactly two visible messages")
                check("native capture acknowledges visible user and assistant messages")
                before = len(append_calls)
                again = capture("repeat_capture")
                require(len(append_calls) == before and again.get("captured") == 2, "Unchanged transcript was appended again")
                check("repeating capture does not append duplicate messages")
                with transcript.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(message("qa-later", *expected[2], moment + timedelta(seconds=1)), ensure_ascii=False) + "\n")
                later = capture("incremental_capture")
                require(later.get("captured") == 3 and later.get("acknowledged") == 3, "Incremental capture did not acknowledge exactly one new message")
                check("later transcript content is captured incrementally")
                shutil.copy2(transcript, output / "synthetic-rollout.jsonl")
                state_file = state_dir / (native_id + ".json")
                if state_file.exists():
                    shutil.copy2(state_file, output / "capture-state.json")

            # Local acknowledgement can precede creation of the server copy. Wait
            # for that copy before asking the server to acknowledge exact records.
            deadline = time.monotonic() + 60
            while True:
                observed = status()
                verify_binding(observed)
                if (observed.get('upload') or {}).get('state') == 'uploaded' or time.monotonic() >= deadline:
                    break
                time.sleep(1)
            report['upload_before_server_replay'] = observed
            require((observed.get('upload') or {}).get('state') == 'uploaded', 'Cloud thread creation was not confirmed')

            # Replay the exact bytes the real core sent, changing only the ack level.
            # Each record must already exist, so a duplicate server receipt proves
            # safe retry plus server acknowledgement without adding another message.
            local_calls = list(append_calls)
            require(local_calls, "Capture made no native append calls")
            server_receipts = []
            for call in local_calls:
                require(call["exit_code"] == 0 and isinstance(call["input"], str), "Initial append was not fully successful")
                command = list(call["args"])
                if "--acknowledge" in command:
                    command[command.index("--acknowledge") + 1] = "server"
                else:
                    command.extend(["--acknowledge", "server"])
                result = observe(command, input=call["input"], text=True, capture_output=True, timeout=60)
                require(result.returncode == 0, "Server acknowledgement replay failed")
                records = [json.loads(line) for line in call["input"].splitlines() if line.strip()]
                receipts = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
                require(len(records) == len(receipts), "Missing server acknowledgement receipt")
                for record, receipt in zip(records, receipts):
                    require(receipt.get("thread_id") == report["thread"] and receipt.get("sequence") == record["sequence"]
                            and receipt.get("disposition") == "duplicate" and receipt.get("duplicate") is True
                            and receipt.get("operation") == "appended" and receipt.get("turn_sequence") is None
                            and isinstance(receipt.get("version"), str) and bool(receipt["version"])
                            and type(receipt.get("next_sequence")) is int and receipt["next_sequence"] > record["sequence"],
                            "Replay did not return a valid duplicate server receipt")
                server_receipts.extend(receipts)
            report["server_receipts"] = server_receipts
            check("unchanged records receive duplicate acknowledgements from the server")

            deadline = time.monotonic() + 30
            while True:
                observed = status()
                verify_binding(observed)
                if (observed.get("upload") or {}).get("state") == "uploaded" or time.monotonic() >= deadline:
                    break
                time.sleep(1)
            report["native_status"] = observed
            require((observed.get("upload") or {}).get("state") == "uploaded", "Cloud upload was not confirmed")
            require(observed.get("visibility") == "private" and (observed.get("access") or {}).get("state") == "granted",
                    "Private cloud access was not confirmed")
            require(observed["producer"].get("next_sequence") == 4, "Unexpected producer sequence after retry")
            check("private cloud upload confirmed with no repository attachments")

            full = json.loads(native("thread", "show", "--format", "full", report["thread"]))
            require(isinstance(full, list) and len(full) == 1 and full[0].get("id") == report["thread"]
                    and len(full[0].get("messages", [])) == 3, "Unexpected transcript size after deduplication")
            readback = json.loads(native("thread", "show", "--format", "messages", "--messages", "1-3", report["thread"]))
            require(isinstance(readback, list) and len(readback) == 1 and readback[0].get("id") == report["thread"],
                    "Readback returned a different thread")
            actual = [(row.get("role"), row.get("buffer", "").rstrip("\n")) for row in readback[0].get("messages", [])]
            require(actual == expected, "Readback differs from the exact synthetic source messages")
            report["readback"] = readback
            check("exact visible messages read back once; excluded analysis is absent")
            report["status"] = "passed"
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as exc:
        report["error"] = str(exc)
        print("FAIL " + str(exc), file=sys.stderr, flush=True)
    finally:
        if report["thread"]:
            try:
                observed = status()
                verify_binding(observed)
                require(observed.get("visibility") in (None, "private"), "Refusing cleanup of a non-private thread")
                native("thread", "archive", report["thread"])
                report["archived"] = True
            except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as exc:
                report.update(status="failed", archive_error=str(exc))
                print("FAIL synthetic thread cleanup: " + str(exc), file=sys.stderr, flush=True)
        report["finished_at"] = timestamp(datetime.now(timezone.utc))
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print("Report: " + str(output / "report.json"), flush=True)
    return 0 if report["status"] == "passed" and report["archived"] else 1


if __name__ == "__main__":
    sys.exit(main())
