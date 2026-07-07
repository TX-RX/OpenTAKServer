#!/usr/bin/env bash
# Apply branch protection to the fork's `main` branch.
# Idempotent — safe to re-run to reconcile state after manual UI changes.
#
# Prereqs:
#   - gh CLI authenticated as an admin on TX-RX/OpenTAKServer
#   - `main` branch already exists on the fork
#
# Usage:
#   bash .github/setup-branch-protection.sh                # applies to TX-RX/OpenTAKServer:main
#   REPO=TX-RX/OpenTAKServer BRANCH=main bash .github/setup-branch-protection.sh

set -euo pipefail

REPO="${REPO:-TX-RX/OpenTAKServer}"
BRANCH="${BRANCH:-main}"

echo "Applying branch protection to $REPO:$BRANCH"

# CI job names that must pass. Update these if the ci.yml job matrix changes.
REQUIRED_CHECKS='["lint","test (3.11)","test (3.12)"]'

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

echo "Done. Verify at: https://github.com/$REPO/settings/branches"
