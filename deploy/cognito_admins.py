"""Provision the closed Field Echo operator pool independently of releases.

No passwords, participant addresses or AWS response bodies are printed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
from pathlib import Path

import boto3
from botocore.exceptions import BotoCoreError, ClientError


class AccessError(Exception):
    """A safe operator-facing failure, containing no participant data."""


def read_addresses(path: Path) -> list[str]:
    """Read explicit addresses from a private file outside Git."""
    path = Path(path).absolute()
    resolved = path.resolve()
    if path.is_symlink() or any((parent / ".git").exists() for parent in resolved.parents):
        raise AccessError("Account list must be a regular private file outside Git")
    try:
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as source:
            info = os.fstat(source.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_uid != os.getuid()
                or info.st_size > 16384
            ):
                raise AccessError("Account list must be owned by this operator with mode 0600")
            data = json.load(source)
    except (OSError, ValueError):
        raise AccessError("Cannot read private account list") from None
    if not isinstance(data, list) or not data:
        raise AccessError("Account list must be a nonempty JSON array of addresses")
    addresses = []
    for value in data:
        if not isinstance(value, str) or len(value) > 254:
            raise AccessError("Account list contains an invalid address")
        local, separator, domain = value.rpartition("@")
        labels = domain.split(".")
        if (
            not separator
            or not re.fullmatch(r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~.-]{1,64}", local)
            or local.startswith(".")
            or local.endswith(".")
            or ".." in local
            or len(labels) < 2
            or any(
                not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
                for label in labels
            )
        ):
            raise AccessError("Account list contains an invalid address")
        addresses.append(f"{local}@{domain.lower()}")
    if len({email.casefold() for email in addresses}) != len(addresses):
        raise AccessError("Account list contains duplicate addresses")
    return addresses


def _pages(client, method, result_key, **kwargs):
    while True:
        response = getattr(client, method)(**kwargs)
        yield from response.get(result_key, [])
        token = response.get("NextToken") or response.get("PaginationToken")
        if not token:
            break
        kwargs["PaginationToken" if method == "list_users" else "NextToken"] = token


def _validate(client, pool_id, client_id, account_id, region):
    if (
        not re.fullmatch(r"\d{12}", account_id)
        or not re.fullmatch(r"[a-z]{2}-[a-z]+-\d", region)
        or not re.fullmatch(re.escape(region) + r"_[A-Za-z0-9]+", pool_id)
        or not re.fullmatch(r"[a-z0-9]+", client_id)
    ):
        raise AccessError("Invalid account, region, pool or client identifier")
    pool = client.describe_user_pool(UserPoolId=pool_id)["UserPool"]
    app = client.describe_user_pool_client(UserPoolId=pool_id, ClientId=client_id)["UserPoolClient"]
    expected_arn = f"arn:aws:cognito-idp:{region}:{account_id}:userpool/{pool_id}"
    if (
        pool.get("Id") != pool_id
        or pool.get("Arn") != expected_arn
        or pool.get("UserPoolTags", {}).get("Application") != "FieldEcho"
        or pool.get("AdminCreateUserConfig", {}).get("AllowAdminCreateUserOnly") is not True
        or pool.get("UsernameAttributes") != ["email"]
        or pool.get("UsernameConfiguration", {}).get("CaseSensitive") is not False
        or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,61}[a-z0-9]", pool.get("Domain", ""))
    ):
        raise AccessError("Pool identity or closed-registration configuration does not match")
    if (
        app.get("ClientId") != client_id
        or app.get("UserPoolId") != pool_id
        or app.get("ClientSecret")
        or app.get("AllowedOAuthFlowsUserPoolClient") is not True
        or app.get("AllowedOAuthFlows") != ["code"]
        or set(app.get("AllowedOAuthScopes", [])) != {"openid", "email"}
        or app.get("SupportedIdentityProviders") != ["COGNITO"]
        or app.get("CallbackURLs") != ["https://fieldecho.org/api/auth/callback"]
        or app.get("LogoutURLs") != ["https://fieldecho.org/"]
    ):
        raise AccessError("Client must be a public code-flow client for the Field Echo origin")
    return pool


def provision(client, path, *, pool_id, client_id, account_id, region, send_invitations=False):
    """Validate the entire boundary before adding missing accounts and labels."""
    addresses = read_addresses(path)
    try:
        pool = _validate(client, pool_id, client_id, account_id, region)
        existing = {}
        for user in _pages(client, "list_users", "Users", UserPoolId=pool_id):
            attributes = {item["Name"]: item["Value"] for item in user.get("Attributes", [])}
            address = attributes.get("email", "").casefold()
            if address not in {email.casefold() for email in addresses} or address in existing:
                raise AccessError(
                    "Pool has unexpected accounts; review membership before provisioning"
                )
            existing[address] = user["Username"]
        memberships = {
            username: {
                group["GroupName"]
                for group in _pages(
                    client,
                    "admin_list_groups_for_user",
                    "Groups",
                    UserPoolId=pool_id,
                    Username=username,
                )
            }
            for username in existing.values()
        }
        try:
            group = client.get_group(UserPoolId=pool_id, GroupName="admins")["Group"]
            if group.get("RoleArn"):
                raise AccessError("The admins label must not grant an AWS IAM role")
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") != "ResourceNotFoundException":
                raise
            client.create_group(
                UserPoolId=pool_id,
                GroupName="admins",
                Description="Field Echo console operators (membership label only)",
            )
        created = added = 0
        for address in addresses:
            username = existing.get(address.casefold())
            if username is None:
                request = {
                    "UserPoolId": pool_id,
                    "Username": address,
                    "UserAttributes": [
                        {"Name": "email", "Value": address},
                        {"Name": "email_verified", "Value": "true"},
                    ],
                }
                if send_invitations:
                    request["DesiredDeliveryMediums"] = ["EMAIL"]
                else:
                    request["MessageAction"] = "SUPPRESS"
                username = client.admin_create_user(**request)["User"]["Username"]
                created += 1
            if "admins" not in memberships.get(username, set()):
                client.admin_add_user_to_group(
                    UserPoolId=pool_id, Username=username, GroupName="admins"
                )
                added += 1
        return {
            "pool_id": pool_id,
            "client_id": client_id,
            "created": created,
            "existing": len(existing),
            "memberships_added": added,
            "environment": {
                "MEL_AUTH_DOMAIN": f"https://{pool['Domain']}.auth.{region}.amazoncognito.com",
                "MEL_AUTH_CLIENT_ID": client_id,
                "MEL_AUTH_ISSUER": f"https://cognito-idp.{region}.amazonaws.com/{pool_id}",
                "MEL_CONSOLE_URL": "https://fieldecho.org",
            },
        }
    except AccessError:
        raise
    except Exception:
        raise AccessError(
            "Cognito operation failed; inspect AWS privately and rerun safely"
        ) from None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--users-file", required=True, type=Path)
    parser.add_argument("--pool-id", required=True)
    parser.add_argument("--client-id", required=True)
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument(
        "--send-invitations",
        action="store_true",
        help="Send email invitations to newly created accounts only",
    )
    args = parser.parse_args(argv)
    try:
        read_addresses(args.users_file)
        result = provision(
            boto3.client("cognito-idp", region_name=args.region),
            args.users_file,
            pool_id=args.pool_id,
            client_id=args.client_id,
            account_id=args.account_id,
            region=args.region,
            send_invitations=args.send_invitations,
        )
    except (AccessError, BotoCoreError):
        parser.exit(1, "Provisioning failed; check private input and closed-pool configuration.\n")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
