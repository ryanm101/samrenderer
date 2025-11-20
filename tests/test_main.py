import pytest
import yaml
from unittest.mock import patch, MagicMock
from samrenderer.main import (
    TemplateRenderer,
    parse_sam_overrides,
    load_sam_config,
    CFNLoader,
    main,
    compare,  # Added import
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
    # Simulate running: sam-render template.yaml
    with patch("sys.argv", ["sam-render", simple_template]):
        main()
        captured = capsys.readouterr()
        # Check if output contains resolved resource ID
        assert "mock-mybucket-id" in captured.out


def test_main_cli_diff(capsys, simple_template, tmp_path):
    """Test the main entrypoint with --env2 to trigger diff mode."""
    # 1. Create a temporary samconfig.toml with two environments
    # FIX: Remove indentation to ensure valid TOML
    config_content = """version = 0.1
[dev.deploy.parameters]
parameter_overrides = "Env=\\\"dev\\\""

[prod.deploy.parameters]
parameter_overrides = "Env=\\\"prod\\\""
"""
    config_file = tmp_path / "samconfig.toml"
    config_file.write_text(config_content, encoding="utf-8")

    # 2. Run CLI with --env dev --env2 prod
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

        # 3. Assert Diff Headers exist
        assert "--- Environment dev" in captured.out
        assert "+++ Environment prod" in captured.out

        # 4. Assert specific changes (IsProd changes from false to true)
        # Note: We look for substrings because exact spacing might vary
        assert "IsProd: false" in captured.out  # Removed line (Red)
        assert "IsProd: true" in captured.out  # Added line (Green)


def test_compare_function():
    """Test the compare logic and ANSI coloring."""
    # Setup two dictionaries that differ
    env1_data = {"Resources": {"Bucket": {"Properties": {"Name": "DevBucket"}}}}
    env2_data = {"Resources": {"Bucket": {"Properties": {"Name": "ProdBucket"}}}}

    # Call compare with [Name, Data] tuples
    output = compare(["dev", env1_data], ["prod", env2_data])

    # Check Headers
    assert "--- Environment dev" in output
    assert "+++ Environment prod" in output

    # Check ANSI Color Codes
    RED = "\033[31m"
    GREEN = "\033[32m"
    RESET = "\033[0m"

    # Verify deletion (DevBucket) is Red
    assert f"{RED}-      Name: DevBucket{RESET}" in output
    # Verify addition (ProdBucket) is Green
    assert f"{GREEN}+      Name: ProdBucket{RESET}" in output


def test_compare_no_diff():
    """Test compare with identical inputs returns empty string."""
    data = {"Resources": {"Bucket": {"Type": "AWS::S3::Bucket"}}}
    # Compare identical data
    output = compare(["dev", data], ["dev", data])
    assert output == ""


def test_sam_config_missing_file():
    """Test graceful failure when config file doesn't exist."""
    # Should return empty dict and print warning to stderr
    assert load_sam_config("nonexistent_file.toml") == {}


def test_sam_config_malformed(tmp_path):
    """Test graceful failure with invalid TOML."""
    f = tmp_path / "bad.toml"
    f.write_text("This is not TOML", encoding="utf-8")
    assert load_sam_config(str(f)) == {}


def test_yaml_loader_complex_tags():
    """Test custom YAML loader handles lists and dicts in tags."""
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

    # Test direct value syntax
    node_direct = {"Fn::FindInMap": ["ConfigMap", "dev", "InvalidKey", 99]}
    assert renderer.resolve(node_direct) == 99


def test_find_in_map_error(renderer):
    """Test missing key without default value returns error string."""
    node = {"Fn::FindInMap": ["ConfigMap", "dev", "Missing"]}
    result = renderer.resolve(node)
    assert "Error: Could not resolve Map" in result


def test_sub_resolution(renderer):
    node = {"Fn::Sub": "Region is ${AWS::Region}"}
    assert renderer.resolve(node) == "Region is us-east-1"


def test_sub_priority(renderer):
    """Ensure variable precedence: Local > Context > Resources > Unknown."""
    renderer.resources["MyRes"] = {}

    # 1. Local overrides everything
    assert renderer.resolve({"Fn::Sub": ["${Var}", {"Var": "local"}]}) == "local"
    # 2. Context (Parameters/Pseudo)
    assert renderer.resolve({"Fn::Sub": "${AWS::Region}"}) == "us-east-1"
    # 3. Resource Mock ID
    assert renderer.resolve({"Fn::Sub": "${MyRes}"}) == "mock-myres-id"
    # 4. Unknown stays as is
    assert renderer.resolve({"Fn::Sub": "${Whoops}"}) == "${Whoops}"


def test_import_value_mock_aws(simple_template):
    """Test Fn::ImportValue with a mocked Boto3 client."""
    with patch("boto3.Session") as mock_session:
        mock_client = MagicMock()
        mock_session.return_value.client.return_value = mock_client

        # Mock AWS response
        mock_client.list_exports.return_value = {
            "Exports": [
                {"Name": "MyExport", "Value": "RealValue"},
                {"Name": "OtherExport", "Value": "OtherValue"},
            ]
        }

        # Initialize with profile to trigger boto3 logic
        r = TemplateRenderer(simple_template, profile="test-profile")

        # Case 1: Export found
        assert r.resolve({"Fn::ImportValue": "MyExport"}) == "RealValue"
        # Case 2: Export not found
        assert r.resolve({"Fn::ImportValue": "Missing"}) == "mock-import-Missing"
        # Case 3: AWS Error (Client fails), fallback to mock
        mock_client.list_exports.side_effect = Exception("AWS Down")
        assert r.resolve({"Fn::ImportValue": "MyExport"}) == "mock-import-MyExport"


def test_split_select_success(renderer):
    """Test successful Split and Select operations."""
    # Split "a,b,c" -> ["a","b","c"], Select index 1 -> "b"
    node = {"Fn::Select": ["1", {"Fn::Split": [",", "a,b,c"]}]}
    assert renderer.resolve(node) == "b"


def test_select_edge_cases(renderer):
    # Index out of bounds
    node = {"Fn::Select": ["5", ["a", "b"]]}
    assert "Error: Select index 5" in renderer.resolve(node)


def test_length_edge_cases(renderer):
    # Not a list
    node = {"Fn::Length": "NotAList"}
    assert renderer.resolve(node) == 0
    # List
    node_list = {"Fn::Length": ["a", "b"]}
    assert renderer.resolve(node_list) == 2


def test_getazs(renderer):
    # Explicit region
    node = {"Fn::GetAZs": "eu-west-1"}
    assert renderer.resolve(node) == ["eu-west-1a", "eu-west-1b", "eu-west-1c"]

    # Implicit region (empty string) -> uses context region
    node_implicit = {"Fn::GetAZs": ""}
    assert renderer.resolve(node_implicit) == ["us-east-1a", "us-east-1b", "us-east-1c"]


def test_condition_missing(renderer):
    """Test referring to a Condition that doesn't exist in the template."""
    assert renderer.resolve({"Condition": "NonExistent"}) is False


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
