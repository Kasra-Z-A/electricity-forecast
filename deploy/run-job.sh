#!/usr/bin/env bash
# Host entry point. Token values must never enter logs or shell tracing.
set -euo pipefail
umask 077
cd /opt/electricity-forecast
# set -a so Compose's variable substitution sees POWER_IMAGE, not just the shell.
set -a
source /etc/power-forecast/host.env
set +a
export AWS_DEFAULT_REGION=${AWS_DEFAULT_REGION:-eu-central-1}
exec 9>/run/power-forecast-host.lock
flock -w 90 9
python3 deploy/record-scheduler.py
job=${1:?job required}
case "$job" in collect|publish|settle|watchdog|dashboard|backup) ;; *) exit 2;; esac
if [[ "$job" == backup ]]; then
  archive=$(mktemp /tmp/power-backup.XXXXXX.tar.gz)
  trap 'rm -f "$archive" "$archive.sha256"' EXIT
  tar -C /var/lib/power-forecast --exclude='./dashboard' --exclude='./.writer.lock' -czf "$archive" .
  sha256sum "$archive" | cut -d' ' -f1 > "$archive.sha256"
  target="s3://$POWER_ARTIFACTS_BUCKET/backups/power/$(date -u +%F).tar.gz"
  aws s3 cp "$archive" "$target" --only-show-errors
  aws s3 cp "$archive.sha256" "$target.sha256" --only-show-errors
  echo 'Archive backup uploaded.'
  exit 0
fi
trap 'rm -f /run/power-forecast.env' EXIT
: > /run/power-forecast.env
if [[ "$job" == collect || "$job" == settle ]]; then
  printf 'ENTSOE_API_KEY=' > /run/power-forecast.env
  aws ssm get-parameter --name "$POWER_TOKEN_PARAMETER" --with-decryption --query Parameter.Value --output text >> /run/power-forecast.env
fi
status=0
: "${POWER_IMAGE:?POWER_IMAGE unset; run deploy/install-server.sh with a digest}"
# Pull only when the digest is absent, so a scheduled job is not blocked on the
# registry once the image is on disk.
if ! docker image inspect "$POWER_IMAGE" >/dev/null 2>&1; then
  aws ecr get-login-password --region "$AWS_DEFAULT_REGION" |
    docker login --username AWS --password-stdin "$POWER_REGISTRY"
  docker pull "$POWER_IMAGE"
  docker logout "$POWER_REGISTRY" >/dev/null
fi
docker compose -f deploy/compose.server.yaml run --rm batch job "$job" || status=$?
# Export fresh status even when the preceding operation failed.
if [[ "$job" != dashboard ]]; then
  docker compose -f deploy/compose.server.yaml run --rm batch job dashboard
elif [[ "$status" != 0 ]]; then
  exit "$status"
fi
aws s3 sync /var/lib/power-forecast/dashboard "s3://$POWER_SITE_BUCKET" \
  --exclude index.html --exclude dashboard.json --cache-control 'public,max-age=300' --only-show-errors
aws s3 cp /var/lib/power-forecast/dashboard/dashboard.json "s3://$POWER_SITE_BUCKET/dashboard.json" \
  --cache-control 'public,max-age=60' --only-show-errors
aws s3 cp /var/lib/power-forecast/dashboard/index.html "s3://$POWER_SITE_BUCKET/index.html" \
  --cache-control 'public,max-age=60' --only-show-errors
exit "$status"
