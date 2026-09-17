#!/usr/bin/env bash
# Run as root after extracting a reviewed release into /opt/electricity-forecast.
set -euo pipefail
cd /opt/electricity-forecast
install -d -m 750 /etc/power-forecast
install -d -o 1000 -g 1000 /var/lib/power-forecast /var/lib/power-forecast/operations
cat > /etc/power-forecast/host.env <<'CONFIG'
AWS_DEFAULT_REGION=eu-central-1
POWER_ARTIFACTS_BUCKET=portfolio-demo-artifacts-<account-id>
POWER_SITE_BUCKET=power-kasra-page-<account-id>
POWER_TOKEN_PARAMETER=/portfolio/power/entsoe-api-key
CONFIG
# Build directly so Compose never rebuilds during a scheduled job.
DOCKER_BUILDKIT=0 docker build -t power-forecast:current .
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
