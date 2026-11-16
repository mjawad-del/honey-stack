# Development

Tests, CI and automatic releases. Back to the [README](../README.md).

Every push or merge to `main` runs the complete CI test matrix, then automatically versions and publishes the tested current revision. Conventional commits select the bump: `feat` produces a minor release, a `!` header or `BREAKING CHANGE:` produces a major release, and other changes produce a patch release. Both plugin manifests, the Python version, changelog, release commit and tag are kept together. If Unreleased is empty, commit subjects provide the release notes.

Repeated release jobs for the same revision are serialized; different revisions cannot cancel each other's pending jobs. If another merge arrives before an older run can release, that older run skips; the newer successful CI run includes those changes in its release. An atomic push prevents concurrent changes from leaving an orphan tag. Rerunning a failed job resumes an already-tagged release instead of bumping again. The repository token publishes the release in the same job, avoiding reliance on another workflow being triggered by a bot push. Failed tests never publish.

The release job needs `contents: write` on `main`; branch rules must permit its version commit. Pull-request tests have read-only repository permissions. Manual versioned tags remain supported, and their publication is also idempotent. Automatic releases become active when this workflow is merged to `main`.

```bash
python3 tests/run.py                    # stdlib self-checks, in parallel (-j N, default: CPU count)
python3 tests/run.py --changed          # only tests the diff vs origin/main can affect
python3 tests/run.py guard ship         # by name: test_guard*.py, test_ship*.py
claude plugin validate .
claude plugin eval . --tag smoke --runs 1 --ablation none --max-cost-usd 2 --no-publish --allow-tools Bash Write Edit
python3 scripts/release.py X.Y.Z --push # optional manual release
```

The runner gives every test script temporary state and a fake model provider. `--changed` maps changed files to the tests that import or drive them, and runs the whole suite when a change touches shared plumbing or can't be mapped; CI always runs everything. `tests/test_hook_imports.py` fails if heavy modules creep back into the tool-call hook path. Direct imports through the test helper also use temporary state when `HONEY_HOME` is unset. Maintained fixture tests run offline; JavaScript regex compatibility is additionally checked when Node is installed. These tests do not run paid model evaluations.

[SemVer](https://semver.org/) + [CHANGELOG](../CHANGELOG.md). Skill style: [docs/skill-style.md](skill-style.md). Design: [docs/design/plan.md](design/plan.md).

MIT © King Umar. Credits and licenses: [THIRD_PARTY.md](../THIRD_PARTY.md).
