#!/usr/bin/env bash
set -euo pipefail

# Run from the project root. Credentials come from the caller's AWS_PROFILE/SSO.
if [[ $# -ne 2 ]]; then
  echo 'Usage: AWS_PROFILE=admin bash deploy/publish-dashboard.sh BUCKET DISTRIBUTION_ID' >&2
  exit 2
fi
site_bucket=$1
site_distribution=$2
uv run --locked power-forecast dashboard --static-preview
aws s3 sync data/dashboard "s3://$site_bucket" --exclude index.html \
  --cache-control 'public,max-age=300' --no-progress
aws s3 cp data/dashboard/index.html "s3://$site_bucket/index.html" \
  --cache-control 'public,max-age=0,must-revalidate' --no-progress
aws cloudfront create-invalidation --distribution-id "$site_distribution" \
  --paths '/*' --query 'Invalidation.[Id,Status]' --output text
