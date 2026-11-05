"""Apply the FieldEcho look to the Cognito managed login page.

The settings and page background live in deploy/cognito-branding/. The logo and
favicon reuse the console's own assets. Run with operator credentials:

    AWS_PROFILE=... uv run python deploy/cognito_branding.py \
        --pool-id POOL_ID --client-id CLIENT_ID --region us-east-1

The style shows only when the pool domain uses managed login version 2
(deploy/auth-bootstrap.yaml). On a new stack, apply it right after the deploy,
before anyone signs in. On a pool still on version 1, apply it before the stack
update, so the new page never renders without it.
"""

import argparse
import json
from pathlib import Path

import boto3

ROOT = Path(__file__).resolve().parent.parent
BRANDING = ROOT / "deploy" / "cognito-branding"
STATIC = ROOT / "src" / "mel" / "explorer" / "static" / "assets"
ASSETS = [
    ("PAGE_BACKGROUND", "JPEG", BRANDING / "page-background.jpg"),
    ("FORM_LOGO", "PNG", STATIC / "icon-192.png"),
    ("FAVICON_ICO", "ICO", STATIC / "favicon.ico"),
]


def request(pool_id: str) -> dict:
    return {
        "UserPoolId": pool_id,
        "UseCognitoProvidedValues": False,
        "Settings": json.loads((BRANDING / "settings.json").read_text()),
        "Assets": [
            {"Category": cat, "ColorMode": "LIGHT", "Extension": ext, "Bytes": path.read_bytes()}
            for cat, ext, path in ASSETS
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pool-id", required=True)
    parser.add_argument("--client-id", required=True)
    parser.add_argument("--region", required=True)
    args = parser.parse_args()
    cognito = boto3.client("cognito-idp", region_name=args.region)
    body = request(args.pool_id)
    try:
        current = cognito.describe_managed_login_branding_by_client(
            UserPoolId=args.pool_id, ClientId=args.client_id
        )["ManagedLoginBranding"]["ManagedLoginBrandingId"]
    except cognito.exceptions.ResourceNotFoundException:
        result = cognito.create_managed_login_branding(ClientId=args.client_id, **body)
    else:
        result = cognito.update_managed_login_branding(ManagedLoginBrandingId=current, **body)
    print(result["ManagedLoginBranding"]["ManagedLoginBrandingId"])


if __name__ == "__main__":
    main()
