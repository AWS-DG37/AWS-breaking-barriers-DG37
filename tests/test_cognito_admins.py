import json
from copy import deepcopy

import pytest
from botocore.exceptions import ClientError

from deploy import cognito_admins as admins

EMAILS = [f"operator{i}@example.test" for i in range(5)]
POOL = "us-east-1_Example"
CLIENT = "exampleclient"
ACCOUNT = "123456789012"


class Cognito:
    def __init__(self):
        self.pool = {
            "Id": POOL,
            "Arn": f"arn:aws:cognito-idp:us-east-1:{ACCOUNT}:userpool/{POOL}",
            "Name": "fieldecho",
            "UserPoolTags": {"Application": "FieldEcho"},
            "AdminCreateUserConfig": {"AllowAdminCreateUserOnly": True},
            "UsernameAttributes": ["email"],
            "UsernameConfiguration": {"CaseSensitive": False},
            "Domain": "fieldecho-example",
        }
        self.client = {
            "ClientId": CLIENT,
            "UserPoolId": POOL,
            "AllowedOAuthFlowsUserPoolClient": True,
            "AllowedOAuthFlows": ["code"],
            "AllowedOAuthScopes": ["openid", "email"],
            "SupportedIdentityProviders": ["COGNITO"],
            "CallbackURLs": ["https://fieldecho.org/api/auth/callback"],
            "LogoutURLs": ["https://fieldecho.org/"],
        }
        self.users = {}
        self.members = set()
        self.groups = set()
        self.creates = []
        self.mutations = []

    def describe_user_pool(self, **kw):
        return {"UserPool": deepcopy(self.pool)}

    def describe_user_pool_client(self, **kw):
        return {"UserPoolClient": deepcopy(self.client)}

    def list_users(self, **kw):
        return {
            "Users": [
                {"Username": name, "Attributes": [{"Name": "email", "Value": email}]}
                for name, email in self.users.items()
            ]
        }

    def get_group(self, **kw):
        if kw["GroupName"] not in self.groups:
            raise ClientError({"Error": {"Code": "ResourceNotFoundException"}}, "GetGroup")
        return {"Group": {"GroupName": kw["GroupName"]}}

    def create_group(self, **kw):
        self.groups.add(kw["GroupName"])
        self.mutations.append("group")
        return {}

    def admin_create_user(self, **kw):
        self.creates.append(kw)
        self.mutations.append("user")
        self.users[kw["Username"]] = kw["Username"]
        return {"User": {"Username": kw["Username"]}}

    def admin_list_groups_for_user(self, **kw):
        return {"Groups": [{"GroupName": "admins"}] if kw["Username"] in self.members else []}

    def admin_add_user_to_group(self, **kw):
        self.members.add(kw["Username"])
        self.mutations.append("membership")
        return {}


@pytest.fixture
def private_list(tmp_path):
    path = tmp_path / "operators.json"
    path.write_text(json.dumps(EMAILS))
    path.chmod(0o600)
    return path


def run(client, path, **kw):
    return admins.provision(
        client, path, pool_id=POOL, client_id=CLIENT, account_id=ACCOUNT, region="us-east-1", **kw
    )


def test_only_missing_users_created_and_repeat_changes_nothing(private_list):
    aws = Cognito()
    aws.users["existing-id"] = EMAILS[0]
    result = run(aws, private_list)
    assert result["created"] == 4
    assert result["existing"] == 1
    assert len(aws.members) == 5
    assert all(c["MessageAction"] == "SUPPRESS" for c in aws.creates)
    assert all("TemporaryPassword" not in c for c in aws.creates)
    before = list(aws.mutations)
    assert run(aws, private_list)["created"] == 0
    assert aws.mutations == before
    assert result["environment"] == {
        "MEL_AUTH_DOMAIN": "https://fieldecho-example.auth.us-east-1.amazoncognito.com",
        "MEL_AUTH_CLIENT_ID": CLIENT,
        "MEL_AUTH_ISSUER": f"https://cognito-idp.us-east-1.amazonaws.com/{POOL}",
        "MEL_CONSOLE_URL": "https://fieldecho.org",
    }
    assert not any(email in json.dumps(result) for email in EMAILS)


def test_invitations_require_explicit_opt_in(private_list):
    aws = Cognito()
    run(aws, private_list, send_invitations=True)
    assert all(
        "MessageAction" not in c and c["DesiredDeliveryMediums"] == ["EMAIL"] for c in aws.creates
    )


@pytest.mark.parametrize("mode", [0o644, 0o400, 0o660])
def test_insecure_file_rejected_before_aws(private_list, mode):
    private_list.chmod(mode)
    with pytest.raises(admins.AccessError):
        run(object(), private_list)


@pytest.mark.parametrize(
    "bad",
    [
        "bad",
        "a@b",
        " a@example.test",
        "a@example.test\n",
        "a+b@example..test",
        "a@-example.test",
        "A@example.test",
    ],
)
def test_invalid_or_duplicate_address_has_no_mutation(private_list, bad):
    private_list.write_text(json.dumps(["a@example.test", bad, *EMAILS[:3]]))
    aws = Cognito()
    with pytest.raises(admins.AccessError):
        run(aws, private_list)
    assert aws.mutations == []


@pytest.mark.parametrize("change", ["account", "signup", "callback", "secret", "flow", "extra"])
def test_unsafe_pool_or_client_rejected_before_mutation(private_list, change):
    aws = Cognito()
    if change == "account":
        aws.pool["Arn"] = aws.pool["Arn"].replace(ACCOUNT, "999999999999")
    elif change == "signup":
        aws.pool["AdminCreateUserConfig"]["AllowAdminCreateUserOnly"] = False
    elif change == "callback":
        aws.client["CallbackURLs"].append("https://other.test/callback")
    elif change == "secret":
        aws.client["ClientSecret"] = "hidden"
    elif change == "flow":
        aws.client["AllowedOAuthFlows"] = ["implicit"]
    else:
        aws.users["stranger"] = "stranger@example.test"
    with pytest.raises(admins.AccessError):
        run(aws, private_list)
    assert aws.mutations == []


def test_aws_errors_do_not_expose_addresses(private_list):
    class Failed(Cognito):
        def admin_create_user(self, **kw):
            raise ClientError(
                {"Error": {"Code": "InvalidParameterException", "Message": EMAILS[0]}},
                "AdminCreateUser",
            )

    with pytest.raises(admins.AccessError) as error:
        run(Failed(), private_list)
    assert EMAILS[0] not in str(error.value)


def test_private_list_inside_git_is_rejected(tmp_path):
    (tmp_path / ".git").mkdir()
    path = tmp_path / "operators.json"
    path.write_text(json.dumps(EMAILS))
    path.chmod(0o600)
    with pytest.raises(admins.AccessError):
        run(object(), path)


def test_authoritative_list_may_have_different_count(private_list):
    private_list.write_text(json.dumps(EMAILS[:2]))
    aws = Cognito()
    assert run(aws, private_list)["created"] == 2
    assert len(aws.users) == 2


def test_empty_list_rejected_before_aws(private_list):
    private_list.write_text("[]")
    with pytest.raises(admins.AccessError):
        run(object(), private_list)


def test_unexpected_client_failure_is_sanitized(private_list):
    class Failed(Cognito):
        def list_users(self, **kw):
            raise RuntimeError(EMAILS[0])

    with pytest.raises(admins.AccessError) as error:
        run(Failed(), private_list)
    assert EMAILS[0] not in str(error.value)


def test_symlinked_directory_cannot_bypass_git_check(tmp_path):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / ".git").mkdir()
    (checkout / "operators.json").write_text(json.dumps(EMAILS))
    (checkout / "operators.json").chmod(0o600)
    (tmp_path / "alias").symlink_to(checkout, target_is_directory=True)
    with pytest.raises(admins.AccessError):
        run(object(), tmp_path / "alias" / "operators.json")


def test_user_inventory_reads_all_pages_before_mutating(private_list):
    class Paged(Cognito):
        def list_users(self, **kw):
            if "PaginationToken" not in kw:
                return {"Users": [], "PaginationToken": "page-two"}
            return {
                "Users": [
                    {
                        "Username": "unexpected",
                        "Attributes": [{"Name": "email", "Value": "unexpected@example.test"}],
                    }
                ]
            }

    aws = Paged()
    with pytest.raises(admins.AccessError):
        run(aws, private_list)
    assert aws.mutations == []


def test_existing_admin_membership_on_later_page_not_added_twice(private_list):
    class Paged(Cognito):
        def admin_list_groups_for_user(self, **kw):
            if "NextToken" not in kw:
                return {"Groups": [], "NextToken": "page-two"}
            return {"Groups": [{"GroupName": "admins"}]}

    aws = Paged()
    aws.groups.add("admins")
    aws.users = {email: email for email in EMAILS}
    assert run(aws, private_list)["memberships_added"] == 0
    assert aws.mutations == []


def test_admins_label_cannot_carry_iam_role(private_list):
    class RoleGroup(Cognito):
        def get_group(self, **kw):
            return {"Group": {"GroupName": "admins", "RoleArn": "arn:aws:iam::123:role/Admin"}}

    aws = RoleGroup()
    with pytest.raises(admins.AccessError):
        run(aws, private_list)
    assert aws.mutations == []


def test_cli_failure_output_contains_no_private_data(private_list, monkeypatch, capsys):
    class Failed(Cognito):
        def admin_create_user(self, **kw):
            raise RuntimeError(EMAILS[0])

    monkeypatch.setattr(admins.boto3, "client", lambda *a, **kw: Failed())
    with pytest.raises(SystemExit) as exit_info:
        admins.main(
            [
                "--users-file",
                str(private_list),
                "--pool-id",
                POOL,
                "--client-id",
                CLIENT,
                "--account-id",
                ACCOUNT,
                "--region",
                "us-east-1",
            ]
        )
    assert exit_info.value.code == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert EMAILS[0] not in output.err
