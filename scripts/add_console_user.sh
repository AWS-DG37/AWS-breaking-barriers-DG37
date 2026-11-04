#!/usr/bin/env bash
# Invite one console user. Cognito emails a temporary password; the user sets a new
# one at first sign-in. Usage: scripts/add_console_user.sh person@example.org
set -euo pipefail
EMAIL="${1:?Usage: scripts/add_console_user.sh EMAIL}"
STACK_NAME="${STACK_NAME:-farmerchat-mel}"
export AWS_DEFAULT_REGION="${AWS_REGION:-us-east-1}"
POOL="$(aws cloudformation describe-stacks --stack-name "$STACK_NAME" \
  --query 'Stacks[0].Outputs[?OutputKey==`UserPoolId`].OutputValue | [0]' --output text)"
aws cognito-idp admin-create-user --user-pool-id "$POOL" --username "$EMAIL" \
  --user-attributes Name=email,Value="$EMAIL" Name=email_verified,Value=true \
  --desired-delivery-mediums EMAIL --query 'User.UserStatus' --output text
