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


# --- NEW TEST: Sub with If Logic ---
def test_sub_with_nested_if(tmp_path):
    """
    Tests !Sub [String, { Var: !If ... }] logic.
    """
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

    # Case 1: IsSpecial = false (Default) -> Suffix should be "dev"
    r1 = TemplateRenderer(str(f))
    res1 = r1.resolve(r1.resources)
    assert res1["TestResource"]["Properties"]["Name"] == "Prefix-dev"

    # Case 2: IsSpecial = true -> Suffix should be "Special"
    r2 = TemplateRenderer(str(f))
    r2.context["IsSpecial"] = "true"  # Override parameter
    res2 = r2.resolve(r2.resources)
    assert res2["TestResource"]["Properties"]["Name"] == "Prefix-Special"


def test_logic_operators(renderer):
    assert renderer.resolve({"Fn::Equals": ["a", "a"]}) is True
    assert renderer.resolve({"Fn::Not": [{"Fn::Equals": ["a", "a"]}]}) is False
    assert renderer.resolve({"Fn::Or": [False, True]}) is True


def test_nested_logic_structure(renderer):
    """
    Tests the specific !And + !Not + !Equals structure requested.
    Equivalent to: Environment == 'dev' AND SubEnvironment != 'none'
    """
    # Case 1: Env=dev, SubEnv=dev1 (Should be True)
    # We mock the inputs manually here since 'renderer' has fixed context
    logic_true = {
        "Fn::And": [
            {"Fn::Equals": ["dev", "dev"]},  # True
            {"Fn::Not": [{"Fn::Equals": ["dev1", "none"]}]},  # Not(False) -> True
        ]
    }
    assert renderer.resolve(logic_true) is True

    # Case 2: Env=prod, SubEnv=dev1 (Should be False because Env != dev)
    logic_false_env = {
        "Fn::And": [
            {"Fn::Equals": ["prod", "dev"]},  # False
            {"Fn::Not": [{"Fn::Equals": ["dev1", "none"]}]},  # True
        ]
    }
    assert renderer.resolve(logic_false_env) is False


def test_condition_evaluation(renderer):
    # IsProd depends on Env=dev (Default), so IsProd should be False
    assert renderer.resolve({"Condition": "IsProd"}) is False
    assert renderer.resolve({"Condition": "IsNotProd"}) is True


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
