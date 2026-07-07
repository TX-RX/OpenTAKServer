#!/usr/bin/env bash
# Establish `main` as the protected collaboration branch on the fork.
#
# Applies:
#   1) Repo-level security controls: Dependabot alerts, automated security
#      fixes, secret scanning + push protection.
#   2) Branch protection on `main`: linear history, required PR review by a
#      CODEOWNER, required status checks (CI + security), no force push,
#      no deletion, resolved conversations.
#
# Idempotent — safe to re-run to reconcile state after any manual UI changes.
#
# Prereqs:
#   - gh CLI authenticated as an admin on TX-RX/OpenTAKServer
#   - `main` branch already exists on the fork
#   - CI, CodeQL, and Security workflows have each run at least once on `main`
#     (GitHub only lets you require a status check as a merge gate after it
#     has been reported by a workflow run).
#
# Usage:
#   bash .github/setup-branch-protection.sh
#   REPO=TX-RX/OpenTAKServer BRANCH=main bash .github/setup-branch-protection.sh

set -euo pipefail

REPO="${REPO:-TX-RX/OpenTAKServer}"
BRANCH="${BRANCH:-main}"

echo "== $REPO =="
echo

# ----------------------------------------------------------------------------
# Repo-level security controls
# ----------------------------------------------------------------------------

echo "[1/5] Enabling Dependabot vulnerability alerts..."
gh api --method PUT -H "Accept: application/vnd.github+json" \
  "/repos/$REPO/vulnerability-alerts" >/dev/null
echo "      ok"

echo "[2/5] Enabling Dependabot automated security fixes..."
gh api --method PUT -H "Accept: application/vnd.github+json" \
  "/repos/$REPO/automated-security-fixes" >/dev/null
echo "      ok"

echo "[3/5] Enabling secret scanning + push protection..."
# Push protection blocks commits containing known secret patterns at git-push
# time, before the code lands on the remote. Requires the fork to be public
# (it is) or have GitHub Advanced Security (not needed for public repos).
gh api --method PATCH -H "Accept: application/vnd.github+json" \
  "/repos/$REPO" \
  -F 'security_and_analysis[secret_scanning][status]=enabled' \
  -F 'security_and_analysis[secret_scanning_push_protection][status]=enabled' \
  >/dev/null
echo "      ok"

# ----------------------------------------------------------------------------
# Merge policy — squash-only. Every PR collapses into one clean commit on
# main, matching the linear-history rule in branch protection below.
# ----------------------------------------------------------------------------

echo "[4/5] Setting squash-only merge policy..."
gh api --method PATCH -H "Accept: application/vnd.github+json" \
  "/repos/$REPO" \
  -F allow_squash_merge=true \
  -F allow_merge_commit=false \
  -F allow_rebase_merge=false \
  -F squash_merge_commit_title=PR_TITLE \
  -F squash_merge_commit_message=PR_BODY \
  -F delete_branch_on_merge=true \
  >/dev/null
echo "      ok"

# ----------------------------------------------------------------------------
# Branch protection on `main`
# ----------------------------------------------------------------------------

echo "[5/5] Applying branch protection to $REPO:$BRANCH..."

# Required check names. These MUST match the workflow job "name:" (or the
# job-id if no name is set) as GitHub reports them. Update this list if
# job names change in ci.yml / codeql.yml / security.yml.
REQUIRED_CHECKS='[
  "lint",
  "test (3.11)",
  "test (3.12)",
  "Analyze (python)",
  "Bandit (Python SAST)",
  "pip-audit (dependency CVEs)",
  "gitleaks (secret scan)"
]'

gh api \
  --method PUT \
  -H "Accept: application/vnd.github+json" \
  "/repos/$REPO/branches/$BRANCH/protection" \
  --input - <<EOF
{
  "required_status_checks": {
    "strict": true,
    "contexts": $REQUIRED_CHECKS
  },
  "enforce_admins": false,
  "required_pull_request_reviews": {
    "dismiss_stale_reviews": true,
    "require_code_owner_reviews": true,
    "required_approving_review_count": 0
  },
  "restrictions": null,
  "required_linear_history": true,
  "allow_force_pushes": false,
  "allow_deletions": false,
  "required_conversation_resolution": true
}
EOF
echo "      ok"

echo
echo "Done."
echo "  Branch protection:   https://github.com/$REPO/settings/branches"
echo "  Security & analysis: https://github.com/$REPO/settings/security_analysis"
