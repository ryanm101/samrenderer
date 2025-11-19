import pytest
from samrenderer.main import TemplateRenderer, parse_sam_overrides


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
    """
    f = tmp_path / "template.yaml"
    f.write_text(content, encoding="utf-8")
    return str(f)


@pytest.fixture
def renderer(simple_template):
    return TemplateRenderer(simple_template, region="us-east-1")


def test_ref_resolution(renderer):
    assert renderer.resolve({"Ref": "Env"}) == "dev"
    assert renderer.resolve({"Ref": "AWS::Region"}) == "us-east-1"
    assert renderer.resolve({"Ref": "MyBucket"}) == "mock-mybucket-id"


def test_find_in_map_standard(renderer):
    node = {"Fn::FindInMap": ["RegionMap", "us-east-1", "AMI"]}
    assert renderer.resolve(node) == "ami-12345"


def test_find_in_map_default_value(renderer):
    node = {"Fn::FindInMap": ["ConfigMap", "dev", "InvalidKey", {"DefaultValue": 10}]}
    assert renderer.resolve(node) == 10


def test_sub_resolution(renderer):
    node = {"Fn::Sub": "Region is ${AWS::Region}"}
    assert renderer.resolve(node) == "Region is us-east-1"


def test_logic_operators(renderer):
    assert renderer.resolve({"Fn::Equals": ["a", "a"]}) is True
    assert renderer.resolve({"Fn::Not": [{"Fn::Equals": ["a", "a"]}]}) is False
    assert renderer.resolve({"Fn::Or": [False, True]}) is True


def test_condition_evaluation(renderer):
    assert renderer.resolve({"Condition": "IsProd"}) is False
    assert renderer.resolve({"Condition": "IsNotProd"}) is True


def test_fn_if_resolution(renderer):
    node = {"Fn::If": ["IsProd", "ProductionValue", "DevValue"]}
    assert renderer.resolve(node) == "DevValue"


# --- New Tests for Added Functions ---


def test_select_and_split(renderer):
    # Split string into list, select 2nd item
    split_node = {"Fn::Split": [",", "a,b,c"]}
    select_node = {"Fn::Select": ["1", split_node]}  # Index 1 = 'b'

    assert renderer.resolve(select_node) == "b"


def test_get_azs(renderer):
    node = {"Fn::GetAZs": {"Ref": "AWS::Region"}}
    # Mock implementation returns [region+a, region+b, region+c]
    expected = ["us-east-1a", "us-east-1b", "us-east-1c"]
    assert renderer.resolve(node) == expected


def test_base64(renderer):
    node = {"Fn::Base64": "UserDataScript"}
    # We render this as a string marker for readability
    assert renderer.resolve(node) == "[Base64: UserDataScript]"


def test_length(renderer):
    node = {"Fn::Length": ["a", "b", "c"]}
    assert renderer.resolve(node) == 3


def test_no_value_removal(renderer):
    # AWS::NoValue should remove keys from dicts
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
