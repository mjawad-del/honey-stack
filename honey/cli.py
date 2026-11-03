"""honey CLI: hook | rsi | delta | install | uninstall | doctor | sync | statusline."""
from __future__ import annotations

import json
import os
import sys


def statusline() -> int:
    """Claude statusLine command: [HONEY] badge plus pending RSI review count."""
    from honey import modes, paths
    from honey.rsi import review, store
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        payload = {}
    st = store.read_json(paths.path("state", "sessions", paths.session_key("claude", payload.get("session_id", "")) + ".json"), {}) or {}
    if not isinstance(st, dict):
        st = {}
    if st.get("off"):
        badge = "[HONEY off]"
    else:
        badge = "[HONEY:" + modes.selected(st, modes.defaults()["mode"]) + "]"
    n = len(review.pending())
    print(badge + (f" rsi:{n}" if n else ""))
    return 0


def main(argv: list) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print("usage: honey install | uninstall | update | autoupdate | doctor | bootstrap --agent claude|codex | task status|set|verify|run | sync | rsi … | delta status|search|read|summary|recall|setup|capture|attach|restore | statusline | --version | hook --agent claude|codex")
        return 0
    cmd, rest = argv[0], argv[1:]
    if cmd == 'bootstrap':
        from honey import activation
        return activation.main(rest)
    if cmd in ("--version", "-V", "version"):
        from honey import __version__
        print(f"honey-stack {__version__}")
        return 0
    if cmd == "hook":
        agent = rest[rest.index("--agent") + 1] if "--agent" in rest else ("codex" if os.environ.get("PLUGIN_DATA") else "claude")
        from honey import dispatch
        return dispatch.run(agent)
    if cmd == "rsi":
        from honey.rsi import cli as rsi_cli
        return rsi_cli.main(rest)
    if cmd == "delta":
        from honey import delta
        return delta.main(rest)
    if cmd == "statusline":
        return statusline()
    if cmd == "autoupdate":
        import argparse
        from honey import autoupdate
        parser = argparse.ArgumentParser(prog="honey autoupdate")
        parser.add_argument("--now", action="store_true", help="check immediately instead of waiting for the hourly interval")
        args = parser.parse_args(rest)
        print(json.dumps(autoupdate.run(force=True) if args.now else autoupdate.run()))
        return 0
    if cmd in ("install", "uninstall", "sync", "update"):
        from honey import install
        return install.main(cmd, rest)
    if cmd == "doctor":
        from honey import doctor
        return doctor.main(rest)
    if cmd == "task":
        from honey import tasks
        return tasks.main(rest)
    print(f"unknown command {cmd}", file=sys.stderr)
    return 2
