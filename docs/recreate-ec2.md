# Recreate private EC2 hosting in your account

Use this opt-in procedure to create your own EC2, PostgreSQL, CloudFront VPC origin,
and closed Cognito console. Your account pays for these resources. For local work
using your own model credits, follow [Run locally with Bedrock](local-bedrock.md).
Application changes still go through a PR to main. The existing main workflow
delivers shared application releases. This procedure does not enable that workflow
for a new account or change its target.

The template and helper have offline tests. Fresh AWS creation, package installation,
SSM execution, Cognito login, origin isolation, reboot, recovery, and delivery
adoption need an operator rehearsal. They have not been established by template lint.

## Check your account and prerequisites

Use AWS CLI v2 with an operator profile in your own account. Keep credentials in
your normal AWS credential provider. Keep private input files outside Git.
Do not put credentials, passwords, participant data, or account lists into the
setup specification or command arguments. Do not enable shell tracing.

Choose a unique namespace such as `alice-demo`. Set these values explicitly in
your operator shell. Substitute your own identifiers throughout this guide.

```sh
export AWS_PROFILE=your-operator-profile
export DG37_ACCOUNT=your-twelve-digit-account
export DG37_REGION=us-east-1
export DG37_NAMESPACE=alice-demo
export DG37_VPC=vpc-your-vpc
export DG37_SUBNET=subnet-your-subnet
export DG37_AMI=ami-your-amazon-linux-2023-x86-64-image
export DG37_DATASET_BUCKET=your-approved-versioned-bucket
export DG37_DATASET_KEY=private/insights.jsonl
export DG37_DATASET_VERSION=your-approved-object-version
```

Run `aws sts get-caller-identity --profile "$AWS_PROFILE"` and compare the account
with `DG37_ACCOUNT`. Before creation, verify all of the following in that account.

- The selected region supports CloudFront VPC origins. The VPC has an attached
  internet gateway, DNS support, and DNS hostnames enabled.
- The subnet belongs to that VPC, has spare IPv4 addresses, and has an IGW route
  for outbound traffic from the host's public IPv4 address. This foundation uses
  the existing outbound network. It adds no NAT gateway or VPC endpoints.
- The AMI is Amazon-owned Amazon Linux 2023, available in the selected region,
  with `x86_64` architecture and the expected root device. It has SSM Agent.
- You can create IAM roles, an instance profile, ECR, EC2, EBS, a VPC origin,
  CloudFront, and Cognito. The operator also needs the describe calls used by
  `ec2_setup.py`, CloudFormation update permission, and root-capable
  `ssm:SendCommand` access to `AWS-RunShellScript` on the new instance.
- The approved dataset is a nonempty JSONL object with a non-null S3 version ID
  and a recorded SHA-256. The bucket policy permits the new host role to read
  that exact version. The template grants only `s3:GetObjectVersion` for that
  object and version. Use SSE-S3 for this procedure. For SSE-KMS, stop and arrange
  a separately reviewed, key-scoped decrypt grant and key policy first.

Use `aws ec2 describe-subnets`, `describe-vpcs`, `describe-vpc-attribute`,
`describe-internet-gateways`, `describe-route-tables`, and `describe-images` with
the explicit region to inspect these prerequisites. Do not open inbound HTTP or
SSH to make an outbound prerequisite work.

## Preview and create the closed foundation

From the repository root, create a change set without executing it.

```sh
aws cloudformation deploy \
  --profile "$AWS_PROFILE" --region "$DG37_REGION" \
  --stack-name "${DG37_NAMESPACE}-foundation" \
  --template-file deploy/ec2-foundation.yaml \
  --capabilities CAPABILITY_IAM \
  --tags "dg37:namespace=$DG37_NAMESPACE" \
  --parameter-overrides \
    "Namespace=$DG37_NAMESPACE" "VpcId=$DG37_VPC" "SubnetId=$DG37_SUBNET" \
    "AmiId=$DG37_AMI" "DatasetBucket=$DG37_DATASET_BUCKET" \
    "DatasetKey=$DG37_DATASET_KEY" "DatasetVersion=$DG37_DATASET_VERSION" \
  --no-execute-changeset
```

Copy the change-set name from that output into `DG37_CHANGE_SET`. Inspect it with
`aws cloudformation describe-change-set --region "$DG37_REGION" --stack-name
"${DG37_NAMESPACE}-foundation" --change-set-name "$DG37_CHANGE_SET"`.
Confirm the namespace, AMI, network, retained volume, and closed host SG. For an
existing stack, stop if the change set replaces the host, volume, attachment,
repository, or retained auth resources. Replacement and recovery are separate
operator operations.

When the preview matches your intended resources, execute and wait for it.

```sh
aws cloudformation execute-change-set --region "$DG37_REGION" \
  --stack-name "${DG37_NAMESPACE}-foundation" --change-set-name "$DG37_CHANGE_SET"
aws cloudformation wait stack-create-complete --region "$DG37_REGION" \
  --stack-name "${DG37_NAMESPACE}-foundation"
aws cloudformation describe-stacks --region "$DG37_REGION" \
  --stack-name "${DG37_NAMESPACE}-foundation" \
  --query 'Stacks[0].Outputs'
```

For a reviewed update, use `stack-update-complete` instead of
`stack-create-complete`. Do not recreate a stack after an uncertain result.
Inspect its status first.

Record the outputs privately. The template creates a namespaced ECR repository,
generated IAM names, an IMDSv2 `t3.small` host, encrypted retained data EBS, and a
private CloudFront origin. UserData installs AL2023 repository packages and
Docker Compose `2.39.4` from Docker's release with its pinned SHA-256. Nginx
returns 503 until bootstrap proves the application ready. The host SG initially
has no ingress.

Use the new `ConsoleUrl` distribution domain by default. For a custom hostname,
supply both `ConsoleHostname` and an already validated matching `CertificateArn`
from the same account in `us-east-1` during preview. Confirm certificate coverage
and status with ACM first. DNS changes remain a separate operator action.
No command here changes shared DNS.

## Create independent sign-in

Set `DG37_CONSOLE_URL` to the exact foundation `ConsoleUrl`, without a trailing
slash. Choose a globally unique Cognito hosted prefix in `DG37_AUTH_PREFIX`.
Preview the independent auth stack with the same deploy pattern.

```sh
aws cloudformation deploy --region "$DG37_REGION" \
  --stack-name "${DG37_NAMESPACE}-auth" \
  --template-file deploy/auth-bootstrap.yaml \
  --parameter-overrides "HostedDomainPrefix=$DG37_AUTH_PREFIX" \
    "ConsoleUrl=$DG37_CONSOLE_URL" --no-execute-changeset
```

Inspect and execute its printed change set, then wait for stack completion.
Read its `UserPoolId`, `UserPoolClientId`, `AuthDomain`, and `AuthIssuer` outputs.
The template's default console remains the existing shared origin. Always pass
your new console explicitly for an independent stack.

Create a mode-0600 JSON array of administrator email addresses outside Git.
Use the existing helper with the new pool, client, account, region, and console.

```sh
uv run python deploy/cognito_admins.py \
  --users-file /private/path/operators.json \
  --pool-id "$DG37_POOL_ID" --client-id "$DG37_CLIENT_ID" \
  --account-id "$DG37_ACCOUNT" --region "$DG37_REGION" \
  --console-url "$DG37_CONSOLE_URL"
```

This validates closed registration and exact callback/logout origins. Add
`--send-invitations` only when you intend to send invitations to newly created
accounts. The setup helper independently validates the matching closed Cognito
configuration before it sends a host command.

## Publish your immutable image and prepare the specification

Check out the exact application commit you intend to run. Build its existing
Dockerfile for `linux/amd64`, with an image label
`org.opencontainers.image.revision` equal to the full 40-character commit.
Authenticate Docker to your foundation's `RepositoryUri` registry through
`aws ecr get-login-password` piped to `docker login --password-stdin`.
Push only to that new repository, using the full commit as its immutable tag.
Run `docker logout` afterward. Read the image digest using
`aws ecr describe-images --repository-name "${DG37_NAMESPACE}-explorer"
--image-ids "imageTag=$DG37_COMMIT" --region "$DG37_REGION"`.
Record the immutable `RepositoryUri@sha256:...` reference.

Create `/private/path/setup.json` with exactly these fields. The identifiers below
are examples, not shared deployment targets. Replace all values with your outputs.

```json
{
  "account": "123456789012",
  "region": "us-east-1",
  "namespace": "alice-demo",
  "vpc_id": "vpc-0123456789abcdef0",
  "subnet_id": "subnet-0123456789abcdef0",
  "image": "123456789012.dkr.ecr.us-east-1.amazonaws.com/alice-demo-explorer@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "commit": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "dataset_bucket": "approved-data",
  "dataset_key": "private/insights.jsonl",
  "dataset_version": "your-approved-version-id",
  "dataset_sha256": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
  "auth_domain": "https://your-unique-prefix.auth.us-east-1.amazoncognito.com",
  "auth_client_id": "yourclientid",
  "auth_issuer": "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_YourPool",
  "console_url": "https://your-distribution.cloudfront.net"
}
```

Keep this specification unchanged for retries. It binds the retained state to the
stack, host, volume, image, dataset, and auth configuration. It contains no AWS
credentials or database password.

## Open only the private origin path and bootstrap

Run the bounded lookup after the VPC origin reports `Deployed`.

```sh
python3 deploy/ec2_setup.py configure-origin --spec /private/path/setup.json
aws cloudformation wait stack-update-complete --region "$DG37_REGION" \
  --stack-name "${DG37_NAMESPACE}-foundation"
python3 deploy/ec2_setup.py bootstrap --spec /private/path/setup.json
python3 deploy/ec2_setup.py status --spec /private/path/setup.json
```

`configure-origin` checks the account, stack outputs, deployed origin, unique
AWS-owned service SG in your VPC, and an applicable service-managed interface in
the subnet. It updates only the ingress parameter and preserves all other stack
parameters. A repeated call with that parameter already set performs no update.
Never create or edit the reserved CloudFront service SG. The only host ingress
rule permits TCP80 from that SG. There is no public custom origin or SSH rule.

`bootstrap` uploads this standalone source through `AWS-RunShellScript` using
base64 and JSON. It prints the SSM command ID before waiting. Save that ID. It
does not add a persistent SSM document or depend on code embedded in the image.
If a submission or wait is uncertain, inspect that command in Systems Manager
before submitting another. Error output omits subprocess bodies and private input.

On the host, an exclusive lock and journal protect `/opt/dg37` on the retained EBS
volume. Bootstrap refuses occupied foreign storage, an unknown nonempty state
root, changed identities, and existing data with missing credentials. A blank
owned volume receives ext4. The database password is generated once on the host
into root-owned mode-0600 env files. It is never placed in the specification,
UserData, command payload, or logs.

Bootstrap checks the dataset version/checksum, stages a UID10001-readable file,
and mounts it read-only. Compose has only PostgreSQL on loopback55437 and the
explorer on host loopback8877 under project `dg37-explorer-cloud`. Env settings
come from `explorer.env`. It runs `Store.initialize` during recoverable initial
setup, starts the explorer with `--no-live`, and starts no worker or job runner.
It sets no spend cap. Only after database, dataset, static assets, and the API's
unauthenticated 401 pass does nginx proxy the app. `COMMIT` and the ready journal
are written last. An interrupted first setup can resume with the same inputs.

Run bootstrap twice and confirm the second run preserves credentials, dataset,
database, Compose, and commit. `status` checks the retained bootstrap identity,
running image revision, effective env, dataset, database, and offline/auth probes.
It does not prove an end-user Cognito login or edge network isolation.

## Verify the new environment before adoption

Use the new HTTPS distribution URL. Confirm static assets load, anonymous API
requests return 401, and an invited operator can sign in and read the database.
Confirm only TCP80 from the service SG reaches the host. Public HTTP, SSH, and
direct API/DB access must fail. Reboot the instance and repeat status and login.
Record the stack outputs, SSM command IDs, image digest, full commit, dataset
version/checksum, and your observations. Do not issue a paid inference call for
bootstrap verification.

Keep local testing on local configuration. Do not edit `deploy/target.json`,
the existing workflow, deployment roles, or live-runner settings to adopt this
host. Adopting it into shared delivery requires a separate reviewed target and
permission change. After `delivery-state.json` exists, bootstrap validates the
preserved release-owned Compose and `COMMIT`; it does not rewrite that release.

## Preserve and recover retained state

Retain is not a backup. Arrange EBS snapshots and PostgreSQL backups, including
the env files, dataset, Compose, commit, bootstrap journal, delivery state, and
backup directory under `/opt/dg37`. Test restore separately. Do not copy secrets
into Git or report logs. Never use `docker compose down -v`, format an occupied
device, regenerate missing credentials, or automatically detach a retained volume.

Host replacement and attachment of an existing retained volume are not automated
by this helper. Stop before a replacement change set. Record the old instance,
volume, filesystem UUID, AZ, and bootstrap identity. Take and verify backups.
Stop application and database writes and obtain an operator-approved recovery
plan. A replacement must use the same AZ and explicitly selected volume. Review
the mount, retained credentials, image, dataset, auth, Compose, commit, and journal
on that volume before any service starts. A changed stack or instance identity
intentionally fails this helper. An operator must reconcile that identity as part
of the reviewed recovery, without initializing or overwriting the retained data.

To retire a test environment, review its CloudFormation delete plan and retained
resources explicitly. EBS, ECR, and Cognito remain after stack deletion. Decide
their retention and backup lifecycle separately. Do not delete a shared stack,
dataset, retained volume, or account membership as part of a local cleanup.

The private-origin ordering follows [CloudFront VPC origin prerequisites](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/private-content-vpc-origins.html).
The pinned plugin uses Docker's [manual Compose installation](https://docs.docker.com/compose/install/linux/)
and [v2.39.4 release checksums](https://github.com/docker/compose/releases/download/v2.39.4/checksums.txt).
