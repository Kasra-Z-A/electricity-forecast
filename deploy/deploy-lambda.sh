#!/usr/bin/env bash
# Point the pipeline functions at an image CI built and pushed. Infrastructure
# comes from the aws-infra power-pipeline stack.
#
#   AWS_PROFILE=admin deploy/deploy-lambda.sh sha256:...
#
# CI prints the digest in the run summary. Rolling back is deploying an older one.
set -euo pipefail

digest="${1:?image digest from CI}"
case "$digest" in
  sha256:*) ;;
  *) echo "pass the digest CI printed (sha256:...), not a tag: $digest" >&2; exit 1 ;;
esac
region="eu-central-1"
account="$(aws sts get-caller-identity --query Account --output text)"
image="$account.dkr.ecr.$region.amazonaws.com/power-pipeline@$digest"
# Both functions run the same image: power-pipeline for the light jobs and
# power-pipeline-models for publish and challengers.
for function in power-pipeline power-pipeline-models; do
  aws lambda update-function-code --region "$region" --function-name "$function" \
    --image-uri "$image" --query LastUpdateStatus --output text
  aws lambda wait function-updated-v2 --region "$region" --function-name "$function"
  echo "deployed $image to $function"
done
