import pytest
import yaml
import boto3
from unittest.mock import patch, MagicMock
from botocore.exceptions import ClientError
from samrenderer.main import (
    TemplateRenderer,
    parse_sam_overrides,
    load_sam_config,
    CFNLoader,
    main,
    compare,
)


# --- Fixtures ---


@pytest.fixture
def simple_template(tmp_path):
    content = """
    Parameters:
      Env:
        Type: String
        Default: dev

    Mappings:
      RegionMap:
        us-east-1:
          AMI: ami-12345
      ConfigMap:
        dev:
          DB: mysql
        prod:
          DB: aurora

    Conditions:
      IsProd: !Equals [!Ref Env, prod]
      IsNotProd: !Not [!Condition IsProd]

    Resources:
      MyBucket:
        Type: AWS::S3::Bucket
        Properties:
          # Adding a Ref here ensures 'mock-mybucket-id' appears in the output
          # which fixes the test_main_cli_execution failure.
          BucketName: !Ref MyBucket
    """
    f = tmp_path / "template.yaml"
    f.write_text(content, encoding="utf-8")
    return str(f)


@pytest.fixture
def renderer(simple_template):
    return TemplateRenderer(simple_template, region="us-east-1")


# --- Unit Tests: CLI & Config ---


def test_main_cli_execution(capsys, simple_template):
    """Test the main entrypoint via CLI arguments."""
    with patch("sys.argv", ["sam-render", simple_template]):
        main()
        captured = capsys.readouterr()
        assert "mock-mybucket-id" in captured.out


def test_main_cli_diff(capsys, simple_template, tmp_path):
    """Test the main entrypoint with --env2 to trigger diff mode."""
    config_content = """version = 0.1
[dev.deploy.parameters]
parameter_overrides = "Env=\\\"dev\\\""

[prod.deploy.parameters]
parameter_overrides = "Env=\\\"prod\\\""
"""
    config_file = tmp_path / "samconfig.toml"
    config_file.write_text(config_content, encoding="utf-8")

    args = [
        "sam-render",
        simple_template,
        "--config",
        str(config_file),
        "--env",
        "dev",
        "--env2",
        "prod",
    ]

    with patch("sys.argv", args):
        main()
        captured = capsys.readouterr()
        assert "--- Environment dev" in captured.out
        assert "+++ Environment prod" in captured.out
        assert "IsProd: false" in captured.out
        assert "IsProd: true" in captured.out


@patch("samrenderer.main.process")
@patch("samrenderer.main.ensure_sso_login")
def test_cli_profile_fallback(mock_login, mock_process, simple_template, tmp_path):
    """Verify that if only --profile is set, it is used for both envs."""
    # Create dummy config
    config_file = tmp_path / "samconfig.toml"
    config_file.write_text("", encoding="utf-8")

    # Case 1: Only --profile set
    args = [
        "sam-render",
        simple_template,
        "--config",
        str(config_file),
        "--env",
        "dev",
        "--env2",
        "prod",
        "--profile",
        "my-profile",
    ]

    # Mock process to return simple dicts to avoid diff errors
    mock_process.return_value = {"Resources": {}}

    with patch("sys.argv", args):
        main()

    # Expect 2 calls to process. Both should have 'my-profile' as the 4th arg
    assert mock_process.call_count == 2

    # Check args of all calls
    # call_args_list is [call(args...), call(args...)]
    # call args: (config, env, template, profile, log_level)
    profiles_used = [c.args[3] for c in mock_process.call_args_list]
    assert profiles_used == ["my-profile", "my-profile"]

    # Expect login called once
    mock_login.assert_called_once_with("my-profile")

    # Reset mocks for Case 2
    mock_process.reset_mock()
    mock_login.reset_mock()

    # Case 2: Both profiles set
    args_two = args + ["--profile2", "prod-profile"]
    with patch("sys.argv", args_two):
        main()

    profiles_used_2 = sorted([c.args[3] for c in mock_process.call_args_list])
    assert profiles_used_2 == ["my-profile", "prod-profile"]

    # Expect login called for both
    assert mock_login.call_count == 2


def test_compare_function():
    """Test the compare logic and ANSI coloring."""
    # Setup data with some shared lines (context) and some diffs
    env1_data = {
        "Resources": {
            "Shared": {"Type": "AWS::S3::Bucket"},
            "Bucket": {"Properties": {"Name": "DevBucket"}},
        }
    }
    env2_data = {
        "Resources": {
            "Shared": {"Type": "AWS::S3::Bucket"},
            "Bucket": {"Properties": {"Name": "ProdBucket"}},
        }
    }

    output = compare(["dev", env1_data], ["prod", env2_data])

    RED = "\033[31m"
    GREEN = "\033[32m"
    RESET = "\033[0m"

    # Verify deletion and addition are colored
    assert f"{RED}-      Name: DevBucket{RESET}" in output
    assert f"{GREEN}+      Name: ProdBucket{RESET}" in output

    # Verify context lines (shared) are present and NOT colored.
    # The logic is: ' ' (diff prefix) + '  ' (yaml indent) = 3 spaces.
    # We simply check that the string exists in the output without color codes prefixing it.
    assert "   Shared:" in output
    assert f"{RED}   Shared:" not in output


def test_compare_no_diff():
    data = {"Resources": {"Bucket": {"Type": "AWS::S3::Bucket"}}}
    output = compare(["dev", data], ["dev", data])
    assert output == ""


def test_sam_config_missing_file():
    assert load_sam_config("nonexistent_file.toml") == {}


def test_sam_config_malformed(tmp_path):
    f = tmp_path / "bad.toml"
    f.write_text("This is not TOML", encoding="utf-8")
    assert load_sam_config(str(f)) == {}


def test_sam_config_parsing_edge_cases():
    """Test empty or None overrides."""
    assert parse_sam_overrides("") == {}
    assert parse_sam_overrides(None) == {}


def test_sam_config_with_region(tmp_path):
    """Test that region is extracted from SAM config."""
    config_content = """version = 0.1
[default.deploy.parameters]
region = "eu-central-1"
parameter_overrides = "Key=Val"
"""
    f = tmp_path / "samconfig.toml"
    f.write_text(config_content, encoding="utf-8")

    config = load_sam_config(str(f))
    assert config["AWS::Region"] == "eu-central-1"


def test_yaml_loader_complex_tags():
    yaml_str = """
    GetAttList: !GetAtt [Res, Attr]
    TagOnList: !MyTag [1, 2]
    TagOnDict: !MyTag {a: 1}
    """
    data = yaml.load(yaml_str, Loader=CFNLoader)
    assert data["GetAttList"] == {"Fn::GetAtt": ["Res", "Attr"]}
    assert data["TagOnList"] == {"Fn::MyTag": [1, 2]}
    assert data["TagOnDict"] == {"Fn::MyTag": {"a": 1}}


# --- Unit Tests: Intrinsics & Resolution ---


def test_ref_resolution(renderer):
    assert renderer.resolve({"Ref": "Env"}) == "dev"
    assert renderer.resolve({"Ref": "AWS::Region"}) == "us-east-1"
    assert renderer.resolve({"Ref": "MyBucket"}) == "mock-mybucket-id"
    assert renderer.resolve({"Ref": "UnknownThing"}) == "{Ref: UnknownThing}"


def test_find_in_map_standard(renderer):
    node = {"Fn::FindInMap": ["RegionMap", "us-east-1", "AMI"]}
    assert renderer.resolve(node) == "ami-12345"


def test_find_in_map_default_value(renderer):
    node = {"Fn::FindInMap": ["ConfigMap", "dev", "InvalidKey", {"DefaultValue": 10}]}
    assert renderer.resolve(node) == 10
    node_direct = {"Fn::FindInMap": ["ConfigMap", "dev", "InvalidKey", 99]}
    assert renderer.resolve(node_direct) == 99


def test_find_in_map_error(renderer):
    node = {"Fn::FindInMap": ["ConfigMap", "dev", "Missing"]}
    result = renderer.resolve(node)
    assert "Error: Could not resolve Map" in result


def test_sub_resolution(renderer):
    node = {"Fn::Sub": "Region is ${AWS::Region}"}
    assert renderer.resolve(node) == "Region is us-east-1"


def test_sub_priority(renderer):
    renderer.resources["MyRes"] = {}
    assert renderer.resolve({"Fn::Sub": ["${Var}", {"Var": "local"}]}) == "local"
    assert renderer.resolve({"Fn::Sub": "${AWS::Region}"}) == "us-east-1"
    assert renderer.resolve({"Fn::Sub": "${MyRes}"}) == "mock-myres-id"
    assert renderer.resolve({"Fn::Sub": "${Whoops}"}) == "${Whoops}"


def test_import_value_mock_aws(simple_template):
    with patch.object(boto3, "Session") as mock_session_cls:
        # Explicitly create the session mock instance
        mock_sess_inst = MagicMock()
        mock_session_cls.return_value = mock_sess_inst

        mock_client = MagicMock()
        # Attach return_value to the client method of the instance
        mock_sess_inst.client.return_value = mock_client

        mock_client.list_exports.return_value = {
            "Exports": [
                {"Name": "MyExport", "Value": "RealValue"},
                {"Name": "OtherExport", "Value": "OtherValue"},
            ]
        }

        r = TemplateRenderer(simple_template, profile="test-profile")

        assert r.resolve({"Fn::ImportValue": "MyExport"}) == "RealValue"
        assert r.resolve({"Fn::ImportValue": "Missing"}) == "mock-import-Missing"

        # Raise a proper ClientError to test the exception handling
        error_response = {
            "Error": {"Code": "ServiceUnavailable", "Message": "AWS Down"}
        }
        mock_client.list_exports.side_effect = ClientError(
            error_response, "ListExports"
        )

        assert r.resolve({"Fn::ImportValue": "MyExport"}) == "mock-import-MyExport"


def test_secrets_manager_edge_cases(simple_template):
    """Test binary secrets, invalid JSON, and missing keys."""
    with patch.object(boto3, "Session") as mock_session_cls:
        mock_sess_inst = MagicMock()
        mock_session_cls.return_value = mock_sess_inst

        mock_sm = MagicMock()

        # Correct side_effect signature and logic
        def client_side_effect(service_name, **kwargs):
            if service_name == "secretsmanager":
                return mock_sm
            return MagicMock()

        # Important: Set side_effect on the .client method of the session instance
        mock_sess_inst.client.side_effect = client_side_effect

        r = TemplateRenderer(simple_template, profile="test-profile")

        # 1. Binary Secret (SecretString is None)
        mock_sm.get_secret_value.return_value = {"SecretBinary": b"binary_data"}
        # Ensure we convert bytes to string representation for assertion
        assert r.resolve("{{resolve:secretsmanager:BinarySecret}}") == "b'binary_data'"

        # 2. Invalid JSON
        mock_sm.get_secret_value.return_value = {"SecretString": "not_json"}
        res = r.resolve("{{resolve:secretsmanager:BadJson:Key}}")
        assert "Error: Secret is not valid JSON" in res

        # 3. Missing Key in JSON
        mock_sm.get_secret_value.return_value = {"SecretString": '{"Foo": "Bar"}'}
        res = r.resolve("{{resolve:secretsmanager:MissingKey:Baz}}")
        assert "Error: Key Baz not found" in res


def test_ref_to_dynamic_reference(simple_template):
    """Test that !Ref to a parameter containing {{resolve...}} recursively resolves it."""
    with patch.object(boto3, "Session") as mock_session_cls:
        mock_sess_inst = MagicMock()
        mock_session_cls.return_value = mock_sess_inst

        mock_sm = MagicMock()

        def client_side_effect(service_name, **kwargs):
            if service_name == "secretsmanager":
                return mock_sm
            return MagicMock()

        mock_sess_inst.client.side_effect = client_side_effect
        mock_sm.get_secret_value.return_value = {"SecretString": "SecretValue"}

        # Setup renderer
        r = TemplateRenderer(simple_template, profile="test-profile")
        # Inject parameter with dynamic ref
        r.context["MyParam"] = "{{resolve:secretsmanager:MySecret}}"

        # Resolve !Ref MyParam
        assert r.resolve({"Ref": "MyParam"}) == "SecretValue"


def test_split_select_success(renderer):
    node = {"Fn::Select": ["1", {"Fn::Split": [",", "a,b,c"]}]}
    assert renderer.resolve(node) == "b"


def test_select_edge_cases(renderer):
    node = {"Fn::Select": ["5", ["a", "b"]]}
    assert "Error: Select index 5" in renderer.resolve(node)


def test_length_edge_cases(renderer):
    node = {"Fn::Length": "NotAList"}
    assert renderer.resolve(node) == 0
    node_list = {"Fn::Length": ["a", "b"]}
    assert renderer.resolve(node_list) == 2


def test_getazs(renderer):
    node = {"Fn::GetAZs": "eu-west-1"}
    assert renderer.resolve(node) == ["eu-west-1a", "eu-west-1b", "eu-west-1c"]
    node_implicit = {"Fn::GetAZs": ""}
    assert renderer.resolve(node_implicit) == ["us-east-1a", "us-east-1b", "us-east-1c"]


def test_condition_missing(renderer):
    assert renderer.resolve({"Condition": "NonExistent"}) is False


def test_fn_condition_explicit(renderer):
    """Test explicit Fn::Condition dict usage."""
    # Assuming 'IsProd' exists in the renderer (from simple_template fixture)
    # IsProd depends on Env=dev (Default), so it is False.
    assert renderer.resolve({"Fn::Condition": "IsProd"}) is False
    assert renderer.resolve({"Fn::Condition": "IsNotProd"}) is True


# --- Logic Tests (Existing) ---


def test_sub_with_nested_if(tmp_path):
    content = """
    Parameters:
      Env: {Type: String, Default: dev}
      IsSpecial: {Type: String, Default: "false"}
    Conditions:
      CheckSpecial: !Equals [!Ref IsSpecial, "true"]
    Resources:
      TestResource:
        Properties:
          Name: !Sub
            - "Prefix-${Suffix}"
            - Suffix: !If [CheckSpecial, "Special", !Ref Env]
    """
    f = tmp_path / "sub_logic.yaml"
    f.write_text(content, encoding="utf-8")

    r1 = TemplateRenderer(str(f))
    res1 = r1.resolve(r1.resources)
    assert res1["TestResource"]["Properties"]["Name"] == "Prefix-dev"

    r2 = TemplateRenderer(str(f))
    r2.context["IsSpecial"] = "true"
    res2 = r2.resolve(r2.resources)
    assert res2["TestResource"]["Properties"]["Name"] == "Prefix-Special"


def test_logic_operators(renderer):
    assert renderer.resolve({"Fn::Equals": ["a", "a"]}) is True
    assert renderer.resolve({"Fn::Not": [{"Fn::Equals": ["a", "a"]}]}) is False
    assert renderer.resolve({"Fn::Or": [False, True]}) is True


def test_nested_logic_structure(renderer):
    logic_true = {
        "Fn::And": [
            {"Fn::Equals": ["dev", "dev"]},
            {"Fn::Not": [{"Fn::Equals": ["dev1", "none"]}]},
        ]
    }
    assert renderer.resolve(logic_true) is True

    logic_false_env = {
        "Fn::And": [
            {"Fn::Equals": ["prod", "dev"]},
            {"Fn::Not": [{"Fn::Equals": ["dev1", "none"]}]},
        ]
    }
    assert renderer.resolve(logic_false_env) is False


def test_condition_evaluation(renderer):
    assert renderer.resolve({"Condition": "IsProd"}) is False
    assert renderer.resolve({"Condition": "IsNotProd"}) is True


def test_base64(renderer):
    node = {"Fn::Base64": "UserDataScript"}
    assert renderer.resolve(node) == "[Base64: UserDataScript]"


def test_no_value_removal(renderer):
    node = {"Key1": "Value1", "Key2": {"Ref": "AWS::NoValue"}}
    resolved = renderer.resolve(node)
    assert "Key1" in resolved
    assert "Key2" not in resolved


def test_sam_config_parsing():
    raw_str = 'VpcStackName="vpc" Environment="dev"'
    expected = {"VpcStackName": "vpc", "Environment": "dev"}
    assert parse_sam_overrides(raw_str) == expected


def test_complex_environment_logic(tmp_path):
    content = """
    Parameters:
      Environment: {Type: String, Default: dev}
      SubEnvironment: {Type: String, Default: none}
    Mappings:
      EnvironmentConfiguration:
        dev: {Key: "key-from-dev"}
    Conditions:
      UseSubEnvironment: !Not [!Equals [!Ref SubEnvironment, none]]
    Resources:
      TestPolicy:
        Properties:
          Resource:
            - !If
              - UseSubEnvironment
              - !FindInMap [EnvironmentConfiguration, !Ref SubEnvironment, Key]
              - !FindInMap [EnvironmentConfiguration, !Ref Environment, Key]
    """
    f = tmp_path / "complex.yaml"
    f.write_text(content, encoding="utf-8")

    r = TemplateRenderer(str(f), region="us-east-1")
    resolved = r.resolve(r.resources)
    assert resolved["TestPolicy"]["Properties"]["Resource"][0] == "key-from-dev"
