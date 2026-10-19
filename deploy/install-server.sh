#!/usr/bin/env bash
# Run as root after extracting a reviewed release into /opt/electricity-forecast.
# Takes the image digest CI published:
#   bash deploy/install-server.sh sha256:<digest>
set -euo pipefail
digest=${1:?image digest required}
# A tag can be moved; a digest cannot. Refuse anything that is not pinned.
case "$digest" in
  sha256:*) ;;
  *) echo 'Refusing an image reference that is not a digest (sha256:...).' >&2; exit 2 ;;
esac
# Registry and bucket names carry the account ID; the host's role tells us which.
account=$(aws sts get-caller-identity --region eu-central-1 --query Account --output text)
registry=$account.dkr.ecr.eu-central-1.amazonaws.com
image=$registry/power-forecast@$digest
cd /opt/electricity-forecast
install -d -m 750 /etc/power-forecast
install -d -o 1000 -g 1000 /var/lib/power-forecast /var/lib/power-forecast/operations
cat > /etc/power-forecast/host.env <<CONFIG
AWS_DEFAULT_REGION=eu-central-1
POWER_ARTIFACTS_BUCKET=portfolio-demo-artifacts-$account
POWER_SITE_BUCKET=power-kasra-page-$account
POWER_TOKEN_PARAMETER=/portfolio/power/entsoe-api-key
POWER_REGISTRY=$registry
POWER_IMAGE=$image
CONFIG
# The image was built and tested in CI. The host only pulls that exact digest,
# so what runs here is what was tested; nothing is built on this machine.
aws ecr get-login-password --region eu-central-1 |
  docker login --username AWS --password-stdin "$registry"
docker pull "$image"
# The token is short-lived, but it should not sit in root's config on disk.
docker logout "$registry" >/dev/null
install -m 644 deploy/systemd/*.timer /etc/systemd/system/
cat > /etc/systemd/system/power-forecast@.service <<'SERVICE'
[Unit]
Description=Electricity forecast %i job and public dashboard update
After=docker.service network-online.target
Requires=docker.service
Wants=network-online.target
[Service]
Type=oneshot
WorkingDirectory=/opt/electricity-forecast
ExecStart=/bin/bash /opt/electricity-forecast/deploy/run-job.sh %i
TimeoutStartSec=20min
SERVICE
systemctl daemon-reload
systemd-analyze verify /etc/systemd/system/power-forecast@.service /etc/systemd/system/power-forecast-*.timer
# Enabling happens after the first collection and export have been verified.
echo 'Image and units installed; timers are not enabled by this script.'
