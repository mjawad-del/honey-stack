"""Maintained behavioral fixtures, independent of a generated RSI candidate.

These are Claude plugin-eval cases. Their file/tool graders test limited observable
behavior; passing them does not establish equivalent behavior in another client.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
from pathlib import Path
import re
import shutil
import tempfile

MANIFEST = "behavioral.json"


def _files(base: Path) -> dict:
    if base.is_symlink() or not base.exists():
        raise ValueError("behavioral fixture is missing or is a symlink: " + str(base))
    files = {}
    paths = [base] if base.is_file() else sorted(base.rglob("*"))
    for path in paths:
        if path.is_symlink():
            raise ValueError("behavioral fixtures cannot contain symlinks")
        if path.is_file():
            files[path.relative_to(base).as_posix() if path != base else path.name] = (
                path.stat().st_mode & 0o777, path.read_bytes())
    return files


def _digest(files: dict) -> str:
    digest = hashlib.sha256()
    for name, (mode, body) in sorted(files.items()):
        digest.update(json.dumps([name, mode, len(body)], separators=(",", ":")).encode() + b"\0")
        digest.update(body)
    return digest.hexdigest()


def case_digest(root: str, name: str, eval_dir: str = "evals") -> str:
    if not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]*", name):
        raise ValueError("invalid behavioral case name")
    return _digest(_files(Path(root, eval_dir, name)))


def snapshot(root: str, eval_dir: str = "evals") -> dict:
    base = Path(root, eval_dir)
    try:
        manifest_files = _files(base / MANIFEST)
        manifest = json.loads((base / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("maintained behavioral manifest is missing or invalid") from exc
    if not isinstance(manifest, dict):
        raise ValueError("unsupported maintained behavioral manifest")
    cases = manifest.get("cases")
    if (manifest.get("schema") != 1 or manifest.get("runner") != "claude-plugin-eval"
            or manifest.get("clients") != ["claude"] or not isinstance(cases, list)
            or not 1 <= len(cases) <= 12 or any(not isinstance(n, str) for n in cases)
            or len(set(cases)) != len(cases)):
        raise ValueError("unsupported maintained behavioral manifest")
    digests = {}
    for name in cases:
        digests[name] = case_digest(root, name, eval_dir)
        folder = base / name
        if not (folder / "prompt.md").is_file() or not list((folder / "graders").glob("*.md")):
            raise ValueError("maintained behavioral case lacks a prompt or graders")
    return {"schema": 1, "runner": "claude-plugin-eval", "clients": ["claude"],
            "manifest_sha256": _digest(manifest_files), "cases": digests}


def validate(evidence: dict) -> None:
    if (not isinstance(evidence, dict) or evidence.get("schema") != 1
            or evidence.get("runner") != "claude-plugin-eval" or evidence.get("clients") != ["claude"]
            or not isinstance(evidence.get("cases"), dict) or not evidence["cases"]
            or not re.fullmatch(r"[0-9a-f]{64}", str(evidence.get("manifest_sha256", "")))
            or any(not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]*", name)
                   or not re.fullmatch(r"[0-9a-f]{64}", str(digest))
                   for name, digest in evidence["cases"].items())):
        raise ValueError("patch lacks valid maintained behavioral evidence; reevaluate it")


def verify(root: str, evidence: dict, eval_dir: str = "evals") -> None:
    validate(evidence)
    if snapshot(root, eval_dir) != evidence:
        raise ValueError("maintained behavioral fixtures changed since evaluation")


def prepare(source: str, worktree: str, skill: str, evidence: dict) -> tuple:
    """Copy trusted cases into a disposable suite; preserve ordinary skill cases."""
    verify(source, evidence)
    base = Path(worktree, "evals")
    base.mkdir(exist_ok=True)
    source_cases = {p.name for p in Path(source, "evals").iterdir()
                    if p.is_dir() and fnmatch.fnmatch(p.name, "*" + skill + "*")
                    and ((p / "prompt.md").is_file() or (p / "case.yaml").is_file())}
    for name in [MANIFEST, *sorted(set(evidence["cases"]) | source_cases)]:
        original, target = Path(source, "evals", name), base / name
        _files(original)  # reject symlinks before copying any selected case
        if target.is_symlink() or target.is_file():
            target.unlink()
        elif target.exists():
            shutil.rmtree(target)
        if original.is_dir():
            shutil.copytree(original, target)
        else:
            shutil.copy2(original, target)
    selected = set(evidence["cases"])
    selected.update(p.name for p in base.iterdir() if p.is_dir()
                    and fnmatch.fnmatch(p.name, "*" + skill + "*")
                    and ((p / "prompt.md").is_file() or (p / "case.yaml").is_file()))
    suite = Path(tempfile.mkdtemp(prefix=".honey-eval-", dir=worktree))
    shutil.copy2(base / MANIFEST, suite / MANIFEST)
    digests = {}
    for name in sorted(selected):
        digests[name] = case_digest(worktree, name)
        shutil.copytree(base / name, suite / name)
    return suite.name, digests, _digest(_files(suite))


def verify_cases(root: str, digests: dict) -> None:
    if any(case_digest(root, name) != digest for name, digest in digests.items()):
        raise ValueError("selected evaluation case or grader fixtures changed")


def verify_suite(root: str, suite: str, digest: str) -> None:
    if _digest(_files(Path(root, suite))) != digest:
        raise ValueError("evaluation case or grader fixtures changed during evaluation")


def verify_application(root: str, evidence: dict, changed: list) -> None:
    maintained = evidence.get("behavioral")
    verify(root, maintained)
    prefixes = ["evals/" + name + "/" for name in maintained["cases"]]
    if any(p == "evals/" + MANIFEST or any(p.startswith(prefix) for prefix in prefixes) for p in changed):
        raise ValueError("candidate cannot modify maintained behavioral fixtures")
    generated = set(evidence.get("generated_cases", []))
    for name, digest in evidence["case_sha256"].items():
        if name not in generated and case_digest(root, name) != digest:
            raise ValueError("selected evaluation fixture changed since evaluation")
