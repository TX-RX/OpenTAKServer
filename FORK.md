# TX-RX/OpenTAKServer — fork notes

This is a downstream fork of [`brian7704/OpenTAKServer`](https://github.com/brian7704/OpenTAKServer)
maintained by TX-RX. It carries a small set of patches that either haven't landed upstream yet or
that upstream has declined to merge. Everything else tracks upstream.

## Branch layout

| Branch  | Purpose                                                                                     |
| ------- | ------------------------------------------------------------------------------------------- |
| `master` | Pristine mirror of upstream `brian7704/OpenTAKServer:master`. Never modified directly.       |
| `main`  | Default branch. `master` + downstream patches. All releases cut from here.                  |
| `feature/*`, `fix/*` | Work branches. PR into `main`. Where possible, also submitted upstream.          |

## Syncing `master` from upstream

`master` should always fast-forward from upstream. If it ever needs a force update, something has
gone wrong — investigate before proceeding.

```bash
git fetch origin
git checkout master
git merge --ff-only origin/master
git push fork master
```

## Rebasing `main` on the new upstream

```bash
git checkout main
git fetch fork
git rebase master     # replay downstream patches onto the new upstream
# resolve any conflicts, then:
git push --force-with-lease fork main
```

Conflicts here are the load-bearing signal that a downstream patch has been overtaken by upstream —
drop the patch if so.

## Current downstream patches

Kept intentionally short. When a patch is submitted upstream, link the PR; when it lands upstream,
delete the row and the patch drops out of `main` on the next rebase.

| Patch                                | Reason downstream                                     | Upstream status |
| ------------------------------------ | ----------------------------------------------------- | --------------- |
| `fix/qr-token-security`              | Enforce single-use enrollment QR by default           | Declined (PR #311) — upstream considers unlimited-use intentional |

## Security gating

Every PR into `main` must pass:

- **CI** — black/isort/flake8/pytest (`.github/workflows/ci.yml`)
- **CodeQL** — Python SAST via GitHub's security-extended query pack (`.github/workflows/codeql.yml`)
- **Bandit** — Python SAST, HIGH severity, baseline-diff against `.bandit-baseline.json` (`.github/workflows/security.yml`)
- **pip-audit** — dependency CVE scan against `poetry.lock`, baseline-diff against `.pip-audit-ignore.txt` (`.github/workflows/security.yml`)
- **gitleaks** — secret pattern scan across the PR range (`.github/workflows/security.yml`)

### Baseline files

Bandit and pip-audit both run in baseline-diff mode. Pre-existing findings against upstream code
and pinned dependencies are recorded as accepted; any NEW finding blocks merge.

- **`.bandit-baseline.json`** — snapshot of current HIGH-severity Bandit findings. Regenerate only
  when the operator has intentionally accepted a new HIGH finding:
  ```bash
  poetry run bandit -r opentakserver -f json -o .bandit-baseline.json --severity-level high
  ```
- **`.pip-audit-ignore.txt`** — one CVE ID per line (comments with `#` allowed). When a Dependabot
  PR upgrades a dep past the vulnerable range, drop the corresponding CVE IDs from this file so
  the gate stays real.

Repo-level controls layered on top:

- **Secret scanning + push protection** — GitHub blocks the push itself if a
  known secret pattern is detected, before the code lands on the remote.
- **Dependabot vulnerability alerts** — surfaces new CVEs against pinned deps.
- **Dependabot automated security fixes** — opens PRs to upgrade vulnerable deps.
- **Squash-only merge policy** — every PR collapses to a single commit on
  `main`. Preserves linear history (required by branch protection) and keeps
  the downstream patch inventory in `FORK.md` easy to line up against
  actual commits. Merge commits and rebase merges are disabled at the repo
  level. Merged PR branches are auto-deleted.

All the above are configured by `.github/setup-branch-protection.sh`. Run it after any change
to the required-check names.

## Deploy verification

Per project convention, non-trivial patches are verified on the Azure OTS install before merging to
`main`:

1. Push the feature branch to `fork`.
2. CI builds a wheel artifact.
3. Deploy the wheel to Azure.
4. Verify the change in the browser / with the affected client.
5. Squash-merge to `main`.

## Releases

See [`RELEASE.md`](./RELEASE.md).
