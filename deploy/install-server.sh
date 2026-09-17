#!/usr/bin/env bash
# Run as root after extracting a reviewed release into /opt/electricity-forecast.
# Takes the image digest CI published:
#   bash deploy/install-server.sh <registry>/power-forecast@sha256:<digest>
set -euo pipefail
image=${1:?image digest required}
# A tag can be moved; a digest cannot. Refuse anything that is not pinned.
case "$image" in
  *@sha256:*) ;;
  *) echo 'Refusing an image reference that is not pinned to a digest.' >&2; exit 2 ;;
esac
registry=${image%%/*}
cd /opt/electricity-forecast
install -d -m 750 /etc/power-forecast
install -d -o 1000 -g 1000 /var/lib/power-forecast /var/lib/power-forecast/operations
cat > /etc/power-forecast/host.env <<CONFIG
AWS_DEFAULT_REGION=eu-central-1
POWER_ARTIFACTS_BUCKET=portfolio-demo-artifacts-<account-id>
POWER_SITE_BUCKET=power-kasra-page-<account-id>
POWER_TOKEN_PARAMETER=/portfolio/power/entsoe-api-key
POWER_REGISTRY=$registry
POWER_IMAGE=$image
CONFIG
# The image was built and tested in CI. The host only pulls that exact digest,
# so what runs here is what was tested; nothing is built on this machine.
aws ecr get-login-password --region eu-central-1 |
  docker login --username AWS --password-stdin "$registry"
docker pull "$image"
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
