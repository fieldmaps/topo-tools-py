# Publishing to PyPI

## One-time setup (already done)

- GitHub Environment `pypi` on this repo, with a required-reviewer protection rule (repo Settings → Environments). Approval is required before the `publish` job in `.github/workflows/publish.yml` can run, even after a Release is published.
- Pending trusted publisher registered at <https://pypi.org/manage/account/publishing/>: project `topo-tools`, owner `fieldmaps`, repo `topo-tools-py`, workflow `publish.yml`, environment `pypi`. This authorizes GitHub Actions to publish via OIDC — no long-lived API token stored anywhere.
- `.github/workflows/publish.yml` triggers only on `release: published`, never on push/tag alone.

None of the above needs to be redone for future releases. It's specific to real PyPI — TestPyPI (below) is a fully separate service with its own account/tokens and isn't wired into this workflow.

## Cutting a real release

1. Bump `version` in `pyproject.toml`.
2. Merge to `main` (release-triggered workflows run from the default branch).
3. GitHub → Releases → Draft a new release → tag it (e.g. `v0.1.1`) → Publish release.
4. Approve the `publish` job in the Actions run (the required-reviewer gate from the environment setup above).

Version numbers, once uploaded, are permanent — PyPI never allows re-uploading the same filename again, even after deletion. Staying in `0.x` (SemVer's "no compatibility promises yet" range) means there's no expectation of a steady cadence or of never breaking the CLI/API between releases.

## Rehearsing on TestPyPI

TestPyPI (test.pypi.org) is a separate index for exactly this: testing packaging/publishing mechanics without touching the real project. Same permanent-filename rule applies there too, so each rehearsal attempt needs its own version bump — use `.devN` suffixes (e.g. `0.1.1.dev1`, `0.1.1.dev2`, ...) to keep these visually distinct from real release versions.

Requires a `.env` file (gitignored) in the repo root with `UV_PUBLISH_TOKEN=pypi-...`, generated at <https://test.pypi.org/manage/account/token/> (account-scoped, since the project doesn't exist there yet on first use).

```bash
# 1. bump version in pyproject.toml, e.g. to 0.1.1.dev1
# 2. build
rm -rf dist && uv build
# 3. publish
set -a; source .env; set +a
uv publish --publish-url https://test.pypi.org/legacy/
# 4. verify install + CLI actually works
rm -rf /tmp/testpypi-check
uv venv /tmp/testpypi-check
uv pip install --python /tmp/testpypi-check \
  --index-url https://test.pypi.org/simple/ \
  --extra-index-url https://pypi.org/simple/ \
  topo-tools==<version>
/tmp/testpypi-check/bin/topo-tools extend --help
```

The `--extra-index-url` fallback is required — TestPyPI doesn't mirror `click`/`duckdb`/`psutil`, only whatever you've published there yourself. Full version history: <https://test.pypi.org/project/topo-tools/#history>.
