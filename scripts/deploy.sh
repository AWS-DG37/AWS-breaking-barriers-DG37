#!/usr/bin/env bash
# Creates AWS resources and pushes the image only when explicitly run by an operator.
set -euo pipefail
cd "$(dirname "$0")/.."
AWS_REGION="${AWS_REGION:-us-east-1}"
BEDROCK_MODEL_ID="${BEDROCK_MODEL_ID:-openai.gpt-6-luna}"
BEDROCK_ALLOWED_MODEL_IDS="${BEDROCK_ALLOWED_MODEL_IDS:-$BEDROCK_MODEL_ID}"
if [[ "$BEDROCK_MODEL_ID" == "openai.gpt-6-luna" && "$AWS_REGION" != "us-east-1" ]]; then
  echo 'GPT-6 Luna on Mantle requires AWS_REGION=us-east-1.' >&2
  exit 1
fi
STACK_NAME="${STACK_NAME:-farmerchat-mel}"
ASSETS_STACK="${STACK_NAME}-assets"
IMAGE_TAG="${IMAGE_TAG:-$(date -u +%Y%m%d%H%M%S)}"
export AWS_DEFAULT_REGION="$AWS_REGION"

aws cloudformation deploy --region "$AWS_REGION" \
  --stack-name "$ASSETS_STACK" --template-file infra/bootstrap.yaml \
  --no-fail-on-empty-changeset

TEMPLATE_BUCKET="$(aws cloudformation describe-stacks --stack-name "$ASSETS_STACK" \
  --query 'Stacks[0].Outputs[?OutputKey==`TemplateBucketName`].OutputValue | [0]' --output text)"
REPOSITORY="$(aws cloudformation describe-stacks --stack-name "$ASSETS_STACK" \
  --query 'Stacks[0].Outputs[?OutputKey==`WorkerRepositoryUri`].OutputValue | [0]' --output text)"
REGISTRY="${REPOSITORY%%/*}"
IMAGE="${REPOSITORY}:${IMAGE_TAG}"
aws ecr get-login-password --region "$AWS_REGION" |
  docker login --username AWS --password-stdin "$REGISTRY"
# Lambda accepts only single-platform image manifests, so skip provenance attestations.
docker build --platform linux/amd64 --provenance=false --tag "$IMAGE" .
docker push "$IMAGE"

# S3 receives the SAM template. ECS images are stored in ECR, not packaged by sam build.
sam deploy --region "$AWS_REGION" --stack-name "$STACK_NAME" \
  --template-file template.yaml --s3-bucket "$TEMPLATE_BUCKET" \
  --s3-prefix "$STACK_NAME" --force-upload --capabilities CAPABILITY_IAM \
  --image-repository "$REPOSITORY" \
  --no-fail-on-empty-changeset \
  --parameter-overrides "ContainerImage=$IMAGE" "BedrockModelId=$BEDROCK_MODEL_ID" \
  "BedrockAllowedModelIds=$BEDROCK_ALLOWED_MODEL_IDS"

# Publish the console UI, then refresh CloudFront. /api/* reaches the API through the
# same distribution, so the UI needs no API address or sign-in settings.
output() {
  aws cloudformation describe-stacks --stack-name "$STACK_NAME" \
    --query "Stacks[0].Outputs[?OutputKey==\`$1\`].OutputValue | [0]" --output text
}
# The image omits datasets; the console loads the participant pack from the data bucket.
aws s3 cp docs/participant-pack/data/insights.jsonl \
  "s3://$(output DataBucketName)/datasets/participant-pack/insights.jsonl"
UI_BUCKET="$(output UiBucketName)"
STATIC=src/mel/explorer/static
aws s3 sync "$STATIC" "s3://$UI_BUCKET/static" --delete --exclude '*.test.mjs' --exclude index.html
aws s3 cp "$STATIC/index.html" "s3://$UI_BUCKET/index.html"
# The voice page at /voice/index.html. /voice/audio/* comes from the data bucket, where
# the worker adds the clips of each survey it accepts; seed it with the repository's clips.
aws s3 sync docs/voice "s3://$UI_BUCKET/voice" --delete --exclude 'audio/*'
aws s3 sync docs/participant-pack/json/scenarios "s3://$UI_BUCKET/participant-pack/json/scenarios" \
  --delete --exclude '*' --include '*/survey.*.json'
aws s3 sync docs/voice/audio "s3://$(output DataBucketName)/voice/audio" --exclude index.json \
  --cache-control 'public, max-age=31536000, immutable'
aws cloudfront create-invalidation --distribution-id "$(output UiDistributionId)" \
  --paths '/*' --query 'Invalidation.Id' --output text
echo "Console: $(output ConsoleUrl)"
