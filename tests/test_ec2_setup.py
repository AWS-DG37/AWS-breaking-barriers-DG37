import base64
import dataclasses
import json
import re
import subprocess
from pathlib import Path

import pytest
from cfnlint.decode import decode

from deploy import ec2_setup as setup


def spec_data():
    return dict(
        account="123456789012",
        region="us-east-1",
        namespace="alice-demo",
        vpc_id="vpc-0123456789abcdef0",
        subnet_id="subnet-0123456789abcdef0",
        image="123456789012.dkr.ecr.us-east-1.amazonaws.com/alice-demo-explorer@sha256:" + "a" * 64,
        commit="b" * 40,
        dataset_bucket="approved-data",
        dataset_key="private/insights.jsonl",
        dataset_version="abc-123_+==",
        dataset_sha256="c" * 64,
        auth_domain="https://alice-demo.auth.us-east-1.amazoncognito.com",
        auth_client_id="abc123",
        auth_issuer="https://cognito-idp.us-east-1.amazonaws.com/us-east-1_Test",
        console_url="https://demo.cloudfront.net",
    )


@pytest.fixture
def spec():
    return setup.SetupSpec.parse(spec_data())


@pytest.fixture
def identity():
    return setup.HostIdentity(
        stack_id="arn:aws:cloudformation:us-east-1:123456789012:stack/alice-demo-foundation/"
        + "a" * 36,
        instance_id="i-0123456789abcdef0",
        volume_id="vol-0123456789abcdef0",
        vpc_origin_id="vo_abc123",
        security_group_id="sg-0123456789abcdef0",
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("namespace", "alice;touch /tmp/injected"),
        ("region", "us-east-1\n"),
        ("image", "other/repo:latest"),
        ("commit", "a" * 7),
        ("dataset_version", "null"),
        ("dataset_key", "../escape"),
        ("dataset_key", "$(id)"),
        ("auth_client_id", ""),
        ("auth_issuer", "https://evil.test/pool"),
        ("console_url", "https://demo.cloudfront.net/extra"),
        ("unexpected", "hidden"),
    ],
)
def test_spec_rejects_unsafe_boundary(field, value):
    data = spec_data()
    data[field] = value
    with pytest.raises(setup.SetupError):
        setup.SetupSpec.parse(data)


def test_spec_is_immutable(spec):
    with pytest.raises(dataclasses.FrozenInstanceError):
        spec.namespace = "other"


def foundation():
    template, errors = decode(str(Path("deploy/ec2-foundation.yaml")))
    assert not errors
    return template


def test_foundation_uses_managed_policies_for_uncached_authenticated_requests():
    behavior = foundation()["Resources"]["Distribution"]["Properties"]["DistributionConfig"][
        "DefaultCacheBehavior"
    ]
    # AWS-owned policy identities are an external contract, not template-local resources.
    assert behavior["CachePolicyId"] == "4135ea2d-6df8-44a3-9df3-4b5a84be39ad"
    assert behavior["OriginRequestPolicyId"] == "b689b0a8-53d0-40ab-baf2-68738e2966ac"


def test_foundation_is_closed_first_private_origin_and_retains_host_state():
    template = foundation()
    resources = template["Resources"]
    assert resources["HostSecurityGroup"]["Properties"].get("SecurityGroupIngress", []) == []
    rule = resources["CloudFrontIngress"]
    assert rule["Condition"] == "HasCloudFrontServiceGroup"
    assert rule["Properties"] == dict(
        GroupId={"Ref": "HostSecurityGroup"},
        IpProtocol="tcp",
        FromPort=80,
        ToPort=80,
        SourceSecurityGroupId={"Ref": "CloudFrontServiceSecurityGroupId"},
    )
    assert template["Parameters"]["CloudFrontServiceSecurityGroupId"]["Default"] == ""
    origin = resources["Distribution"]["Properties"]["DistributionConfig"]["Origins"][0]
    assert origin["DomainName"] == {"Fn::GetAtt": ["Host", "PrivateDnsName"]}
    assert "CustomOriginConfig" not in origin
    assert origin["VpcOriginConfig"]["VpcOriginId"] == {"Fn::GetAtt": ["VpcOrigin", "Id"]}
    assert resources["VpcOrigin"]["Properties"]["VpcOriginEndpointConfig"]["Arn"] == {
        "Fn::Sub": "arn:${AWS::Partition}:ec2:${AWS::Region}:${AWS::AccountId}:instance/${Host}"
    }
    assert resources["DataVolume"]["DeletionPolicy"] == "Retain"
    assert resources["DataVolume"]["UpdateReplacePolicy"] == "Retain"
    assert resources["DataVolume"]["Properties"]["Encrypted"] is True
    assert resources["Host"]["Properties"]["MetadataOptions"] == dict(
        HttpEndpoint="enabled",
        HttpTokens="required",
        HttpPutResponseHopLimit=1,
    )
    assert "CloudFrontIngress" not in json.dumps(resources["Host"])
    assert "CloudFrontIngress" not in json.dumps(resources["VpcOrigin"])


class Cloud:
    def __init__(self, spec, identity):
        self.spec, self.identity, self.calls = spec, identity, []
        self.groups = [
            dict(
                GroupId="sg-11111111111111111",
                VpcId=spec.vpc_id,
                OwnerId=spec.account,
                GroupName="CloudFront-VPCOrigins-Service-SG",
            )
        ]
        self.parameters = [
            dict(ParameterKey="Namespace", ParameterValue=spec.namespace),
            dict(ParameterKey="CloudFrontServiceSecurityGroupId", ParameterValue=""),
            dict(ParameterKey="DatasetKey", ParameterValue=spec.dataset_key),
            dict(ParameterKey="DatasetBucket", ParameterValue=spec.dataset_bucket),
            dict(ParameterKey="DatasetVersion", ParameterValue=spec.dataset_version),
            dict(ParameterKey="VpcId", ParameterValue=spec.vpc_id),
            dict(ParameterKey="SubnetId", ParameterValue=spec.subnet_id),
        ]

    def __call__(self, *args):
        self.calls.append(args)
        if args[:2] == ("sts", "get-caller-identity"):
            return {"Account": self.spec.account}
        if args[:2] == ("cloudformation", "describe-stacks"):
            return {
                "Stacks": [
                    dict(
                        StackId=self.identity.stack_id,
                        StackStatus="CREATE_COMPLETE",
                        Tags=[dict(Key="dg37:namespace", Value=self.spec.namespace)],
                        Parameters=self.parameters,
                        Outputs=[
                            dict(OutputKey=k, OutputValue=v)
                            for k, v in {
                                "InstanceId": self.identity.instance_id,
                                "VolumeId": self.identity.volume_id,
                                "VpcOriginId": self.identity.vpc_origin_id,
                                "HostSecurityGroupId": self.identity.security_group_id,
                                "VpcId": self.spec.vpc_id,
                                "SubnetId": self.spec.subnet_id,
                                "RepositoryUri": self.spec.image.split("@")[0],
                                "ConsoleUrl": self.spec.console_url,
                            }.items()
                        ],
                    )
                ]
            }
        if args[:2] == ("cloudfront", "get-vpc-origin"):
            return {
                "VpcOrigin": dict(
                    Status="Deployed",
                    VpcOriginEndpointConfig=dict(
                        Arn=f"arn:aws:ec2:{self.spec.region}:{self.spec.account}:instance/{self.identity.instance_id}"
                    ),
                )
            }
        if args[:2] == ("ec2", "describe-security-groups"):
            return {"SecurityGroups": self.groups}
        if args[:2] == ("ec2", "describe-network-interfaces"):
            return {
                "NetworkInterfaces": [
                    dict(
                        VpcId=self.spec.vpc_id,
                        SubnetId=self.spec.subnet_id,
                        RequesterManaged=True,
                        Groups=[dict(GroupId=self.groups[0]["GroupId"])],
                    )
                ]
            }
        if args[:2] == ("cloudformation", "update-stack"):
            return {"StackId": self.identity.stack_id}
        raise AssertionError(args)


def test_origin_lookup_preserves_every_other_stack_parameter(spec, identity):
    aws = Cloud(spec, identity)
    assert setup.configure_origin(spec, aws) == "sg-11111111111111111"
    call = next(c for c in aws.calls if c[:2] == ("cloudformation", "update-stack"))
    parameters = json.loads(call[call.index("--parameters") + 1])
    assert parameters == [
        dict(ParameterKey="Namespace", UsePreviousValue=True),
        dict(
            ParameterKey="CloudFrontServiceSecurityGroupId", ParameterValue="sg-11111111111111111"
        ),
        dict(ParameterKey="DatasetKey", UsePreviousValue=True),
        dict(ParameterKey="DatasetBucket", UsePreviousValue=True),
        dict(ParameterKey="DatasetVersion", UsePreviousValue=True),
        dict(ParameterKey="VpcId", UsePreviousValue=True),
        dict(ParameterKey="SubnetId", UsePreviousValue=True),
    ]
    assert "--use-previous-template" in call


@pytest.mark.parametrize(
    "case", ["missing", "ambiguous", "wrong-vpc", "wrong-owner", "reserved-name"]
)
def test_origin_lookup_refuses_uncertain_service_group(spec, identity, case):
    aws = Cloud(spec, identity)
    if case == "missing":
        aws.groups = []
    elif case == "ambiguous":
        aws.groups *= 2
    elif case == "wrong-vpc":
        aws.groups[0]["VpcId"] = "vpc-99999999999999999"
    elif case == "wrong-owner":
        aws.groups[0]["OwnerId"] = "999999999999"
    else:
        aws.groups[0]["GroupName"] = "pretend-cloudfront"
    with pytest.raises(setup.SetupError):
        setup.configure_origin(spec, aws)
    assert not any(c[:2] == ("cloudformation", "update-stack") for c in aws.calls)


def test_origin_rerun_does_not_update_stack(spec, identity):
    aws = Cloud(spec, identity)
    aws.parameters[1]["ParameterValue"] = "sg-11111111111111111"
    setup.configure_origin(spec, aws)
    assert not any(c[:2] == ("cloudformation", "update-stack") for c in aws.calls)


def test_stack_dataset_mismatch_is_refused_before_ingress_or_host(spec, identity):
    aws = Cloud(spec, identity)
    aws.parameters[4]["ParameterValue"] = "unapproved-version"
    with pytest.raises(setup.SetupError):
        setup.configure_origin(spec, aws)
    assert not any(c[0] in ("ec2", "ssm") for c in aws.calls)


class Host:
    def __init__(self, fail=None):
        self.calls, self.fail = [], fail

    def mount(self, volume, root):
        self.calls.append("mount")

    def prepare(self, spec, root):
        self.calls.append("prepare")
        data = root / "data" / "insights.jsonl"
        data.write_text("approved dataset")
        (root / "postgres" / "PG_VERSION").write_text("16")

    def migrate(self, spec, root):
        self.calls.append("migrate")
        if self.fail == "migrate":
            raise setup.SetupError("interrupted")

    def verify(self, spec, root, adopted=False):
        self.calls.append("verify-adopted" if adopted else "verify")
        if self.fail == "verify":
            raise setup.SetupError("probe failed")

    def proxy(self, spec, root):
        self.calls.append("proxy")


def test_bootstrap_twice_preserves_credentials_data_and_commit(spec, identity, tmp_path):
    root = tmp_path / "state"
    host = Host()
    assert setup.bootstrap_host(spec, identity, root, host) == "ready"
    saved = {p.name: p.read_bytes() for p in root.iterdir() if p.is_file()}
    password = setup.read_env(root / "database.env")["POSTGRES_PASSWORD"]
    assert len(password) >= 32
    assert (root / "explorer.env").stat().st_mode & 0o777 == 0o600
    assert setup.read_env(root / "explorer.env")["MEL_DATABASE_PASSWORD"] == password
    host.calls.clear()
    assert setup.bootstrap_host(spec, identity, root, host) == "ready"
    assert host.calls == ["mount", "verify"]
    assert saved == {p.name: p.read_bytes() for p in root.iterdir() if p.is_file()}


def test_interruption_resumes_without_new_password_or_premature_commit(spec, identity, tmp_path):
    root = tmp_path / "state"
    with pytest.raises(setup.SetupError):
        setup.bootstrap_host(spec, identity, root, Host(fail="migrate"))
    password = (root / "database.env").read_bytes()
    assert not (root / "COMMIT").exists()
    assert json.loads((root / "bootstrap-state.json").read_text())["phase"] == "preparing"
    setup.bootstrap_host(spec, identity, root, Host())
    assert (root / "database.env").read_bytes() == password
    assert (root / "COMMIT").read_text() == spec.commit + "\n"


@pytest.mark.parametrize("case", ["foreign-root", "missing-password", "changed-identity"])
def test_bootstrap_refuses_foreign_or_incomplete_state(spec, identity, tmp_path, case):
    root = tmp_path / "state"
    if case == "foreign-root":
        root.mkdir()
        (root / "unknown").write_text("do not touch")
    else:
        setup.bootstrap_host(spec, identity, root, Host())
        if case == "missing-password":
            (root / "database.env").unlink()
        else:
            identity = dataclasses.replace(identity, volume_id="vol-99999999999999999")
    before = {str(p): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    with pytest.raises(setup.SetupError):
        setup.bootstrap_host(spec, identity, root, Host())
    assert before == {str(p): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_delivery_adoption_is_validation_only(spec, identity, tmp_path):
    root = tmp_path / "state"
    setup.bootstrap_host(spec, identity, root, Host())
    (root / "delivery-state.json").write_text('{"release_owned":true}')
    (root / "COMMIT").write_text("d" * 40 + "\n")
    (root / "compose.yaml").write_text("release-owned")
    before = {str(p): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    host = Host()
    assert setup.bootstrap_host(spec, identity, root, host) == "delivery-owned"
    assert host.calls == ["mount", "verify-adopted"]
    assert before == {str(p): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_compose_matches_existing_delivery_contract(spec):
    root = Path("/opt/dg37")
    config = setup.compose(spec, root)
    assert set(config["services"]) == {"postgres", "explorer"}
    app = config["services"]["explorer"]
    assert app["network_mode"] == "host"
    assert not app.get("ports") and not app.get("environment")
    assert app["env_file"] == ["/opt/dg37/explorer.env"]
    assert app["entrypoint"] == [
        "mel-explorer",
        "--host",
        "127.0.0.1",
        "--port",
        "8877",
        "--no-live",
    ]
    assert app["volumes"][0]["read_only"] is True
    assert config["services"]["postgres"]["ports"] == ["127.0.0.1:55437:5432"]
    assert config["services"]["postgres"]["volumes"][0]["source"] == "/opt/dg37/postgres"


def test_transport_is_fixed_base64_source_and_json(spec, identity):
    parameters = setup.ssm_parameters(spec, identity, "bootstrap")
    script = parameters["commands"][0]
    assert spec.dataset_key not in script and spec.image not in script
    assert "base64.b64decode" in script
    encoded = re.search(r"payload = base64.b64decode\('([A-Za-z0-9+/=]+)'\)", script)[1]
    payload = json.loads(base64.b64decode(encoded))
    assert payload == dict(spec=dataclasses.asdict(spec), identity=dataclasses.asdict(identity))
    assert parameters["executionTimeout"] == ["1800"]


def test_process_failure_output_is_sanitized(monkeypatch):
    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(1, args, output="PASSWORD", stderr="PRIVATE_DATA")

    monkeypatch.setattr(setup.subprocess, "run", fail)
    with pytest.raises(setup.SetupError) as error:
        setup.command(["false"])
    assert "PASSWORD" not in str(error.value) and "PRIVATE_DATA" not in str(error.value)


@pytest.mark.parametrize("case", ["signature", "partition", "mounted", "filesystem"])
def test_occupied_volume_is_never_formatted(monkeypatch, tmp_path, case):
    device = dict(
        name="/dev/nvme1n1", serial="vol0123456789abcdef0", type="disk", mountpoints=[], fstype=None
    )
    if case == "partition":
        device["children"] = [dict(name="/dev/nvme1n1p1")]
    elif case == "mounted":
        device["mountpoints"] = ["/somewhere-else"]
    elif case == "filesystem":
        device["fstype"] = "xfs"
    calls = []

    def execute(args, **kwargs):
        calls.append(args)
        if args[0] == "lsblk":
            return json.dumps(dict(blockdevices=[device]))
        if args[0] == "wipefs":
            return json.dumps(dict(signatures=[dict(type="foreign")]))
        raise AssertionError(args)

    monkeypatch.setattr(setup, "command", execute)
    with pytest.raises(setup.SetupError):
        setup.DockerHost().mount("vol-0123456789abcdef0", tmp_path / "state")
    assert not any(call[0] in ("mkfs.ext4", "mount") for call in calls)


def test_fixed_transport_runs_the_uploaded_source_with_exact_payload(
    spec, identity, tmp_path, monkeypatch
):
    artifact = tmp_path / "source.py"
    artifact.write_text(
        "import json,sys\np=json.load(sys.stdin)\n"
        "assert p['spec']['namespace']=='alice-demo'\n"
        "assert sys.argv[1]=='_host-status'\nprint('transport-ok')\n"
    )
    monkeypatch.setattr(setup, "__file__", str(artifact))
    script = setup.ssm_parameters(spec, identity, "status")["commands"][0]
    result = subprocess.run(["/bin/sh"], input=script, text=True, capture_output=True, check=True)
    assert result.stdout == "transport-ok\n" and result.stderr == ""


def test_cli_failure_captures_no_subprocess_or_private_input(tmp_path, monkeypatch, capsys):
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(spec_data()))

    def fail(spec):
        raise setup.SetupError("SECRET_VALUE participant@example.test")

    monkeypatch.setattr(setup, "aws_cli", fail)
    with pytest.raises(SystemExit) as result:
        setup.main(["bootstrap", "--spec", str(path)])
    assert result.value.code == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert "SECRET_VALUE" not in output.err and "participant@example.test" not in output.err


def test_auth_template_preserves_shared_default_and_selects_new_origin():
    template, errors = decode("deploy/auth-bootstrap.yaml")
    assert not errors
    assert template["Parameters"]["ConsoleUrl"]["Default"] == "https://fieldecho.org"
    properties = template["Resources"]["UserPoolClient"]["Properties"]
    assert properties["CallbackURLs"] == [{"Fn::Sub": "${ConsoleUrl}/api/auth/callback"}]
    assert properties["LogoutURLs"] == [{"Fn::Sub": "${ConsoleUrl}/"}]


@pytest.mark.parametrize(
    "case", ["valid", "wrong-account", "open-signup", "wrong-callback", "secret"]
)
def test_auth_cloud_boundary_requires_closed_matching_pool_before_host(spec, case):
    pool_id = "us-east-1_Test"
    pool = dict(
        Arn=f"arn:aws:cognito-idp:us-east-1:{spec.account}:userpool/{pool_id}",
        Domain="alice-demo",
        AdminCreateUserConfig=dict(AllowAdminCreateUserOnly=True),
    )
    client = dict(
        UserPoolId=pool_id,
        ClientId=spec.auth_client_id,
        AllowedOAuthFlowsUserPoolClient=True,
        AllowedOAuthFlows=["code"],
        AllowedOAuthScopes=["openid", "email"],
        SupportedIdentityProviders=["COGNITO"],
        CallbackURLs=[spec.console_url + "/api/auth/callback"],
        LogoutURLs=[spec.console_url + "/"],
    )
    if case == "wrong-account":
        pool["Arn"] = pool["Arn"].replace(spec.account, "999999999999")
    elif case == "open-signup":
        pool["AdminCreateUserConfig"]["AllowAdminCreateUserOnly"] = False
    elif case == "wrong-callback":
        client["CallbackURLs"] = ["https://foreign.test/callback"]
    elif case == "secret":
        client["ClientSecret"] = "SECRET"
    calls = []

    def aws(*args):
        calls.append(args)
        if args[:2] == ("cognito-idp", "describe-user-pool"):
            return dict(UserPool=pool)
        if args[:2] == ("cognito-idp", "describe-user-pool-client"):
            return dict(UserPoolClient=client)
        raise AssertionError(args)

    if case == "valid":
        setup.validate_auth(spec, aws)
    else:
        with pytest.raises(setup.SetupError):
            setup.validate_auth(spec, aws)
    assert len(calls) == 2


def test_failed_probe_never_enables_proxy_or_commits(spec, identity, tmp_path):
    host = Host(fail="verify")
    root = tmp_path / "state"
    with pytest.raises(setup.SetupError):
        setup.bootstrap_host(spec, identity, root, host)
    assert "proxy" not in host.calls
    assert not (root / "COMMIT").exists()
    assert json.loads((root / "bootstrap-state.json").read_text())["phase"] == "preparing"


def test_dataset_hash_refuses_symlink_without_reading_target(tmp_path):
    source = tmp_path / "private"
    source.write_text("private")
    link = tmp_path / "dataset"
    link.symlink_to(source)
    with pytest.raises(setup.SetupError):
        setup.file_sha256(link)


def test_recovered_files_in_lost_found_are_foreign_state(spec, identity, tmp_path):
    root = tmp_path / "state"
    lost = root / "lost+found"
    lost.mkdir(parents=True)
    (lost / "recovered").write_text("preserve")
    with pytest.raises(setup.SetupError):
        setup.bootstrap_host(spec, identity, root, Host())
    assert (lost / "recovered").read_text() == "preserve"
    assert not (root / "bootstrap-state.json").exists()
