# Release process

## Versioning

Version numbers are derived from git tags by
[`poetry-dynamic-versioning`](https://github.com/mtkennerly/poetry-dynamic-versioning). The regex in
`pyproject.toml` (`(?P<base>\d+(\.\d+)*)`) only accepts numeric segments, so we can't use PEP 440
local versions like `1.7.11+txrx.1` without modifying `pyproject.toml` — which would conflict with
every upstream sync.

**Scheme:** 4-part semver, where the 4th segment is the downstream patch counter.

- `1.7.11` — upstream release (their tag, unmodified)
- `1.7.11.1` — first TX-RX downstream release on top of upstream `1.7.11`
- `1.7.11.2` — second downstream release on top of the same upstream base
- `1.7.12` (upstream) → reset counter → `1.7.12.1` for the next downstream release

Bump the 4th segment for any release from this fork, even docs/CI-only releases, so downstream
consumers can pin exactly.

## Cutting a release

1. Confirm `main` is green on CI and the desired commit is on `main`.
2. Confirm the deploy has been verified on Azure OTS (per `FORK.md`).
3. Tag and push:
   ```bash
   git checkout main
   git pull fork main
   git tag 1.7.11.1     # no `v` prefix — poetry-dynamic-versioning matches numeric tags
   git push fork 1.7.11.1
   ```
4. `release.yml` fires on the tag: builds sdist/wheel, uploads them to a GitHub Release with
   auto-generated notes.
5. `docker.yml` fires on the GitHub Release publish and pushes container images to `ghcr.io`.

   Note: the upstream `docker.yml` matrix pushes to `ghcr.io/brian7704/*` — hard-coded, not derived
   from `github.repository`. If you want fork images at `ghcr.io/tx-rx/*`, patch that matrix on
   `main`. Left alone for now to keep the docker workflow in sync with upstream.

## If the release fails

- Wheel build mismatched the tag: `release.yml` fails fast with the resolved version. Delete the
  tag on the fork remote, fix the underlying issue, re-tag.
  ```bash
  git push fork :refs/tags/1.7.11.1
  git tag -d 1.7.11.1
  ```
- Docker publish failed but wheel is out: re-run `docker.yml` via workflow_dispatch. The GitHub
  Release itself is fine.

## Do not

- **Do not** move an existing tag. Cut a new one instead.
- **Do not** publish to PyPI. This is a fork; PyPI would collide with upstream's `OpenTAKServer`
  package name.
- **Do not** tag from `master` — that would attach a downstream version to a pristine upstream
  commit and confuse anyone who diffs.
