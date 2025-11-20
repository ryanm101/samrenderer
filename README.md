# **SAM Template Renderer**

A lightweight Python tool to parse, resolve, and render AWS SAM and CloudFormation templates locally.

This tool is designed to help debug complex template logic—specifically **Mappings**, **Conditions**, and **Substitutions**—without needing to deploy to AWS. It resolves intrinsic functions locally and outputs the final "rendered" YAML.

## **Features**

* **Intrinsic Function Resolution:** Evaluates `Fn::FindInMap`, `Fn::If`, `Fn::Sub`, `Fn::Join`, `Fn::Select`, `Fn::Split`, and more locally.
* **Logic Handling:** Fully supports boolean logic (`Fn::And`, `Fn::Or`, `Fn::Not`, `Fn::Equals`) to correctly evaluate Condition blocks.
* **Environment Diffing:** Compare the rendered output of two different environments (e.g., dev vs prod) to visualize configuration differences.
* **Dynamic References:** Resolves `{{resolve:secretsmanager:...}}` patterns when an AWS profile is active.
* **SAM Config Support:** Parses `samconfig.toml` to apply environment-specific parameter_overrides automatically.
* **Custom YAML Tags:** Handles short-form CloudFormation tags (e.g., `!Ref`, `!Sub`, `!GetAtt`) without parsing errors.
* **Hybrid Resolution:** Mocks runtime values (like Resource IDs) but can optionally fetch real values from AWS (Imports, Secrets) if a profile is provided.
* **Extended Syntax:** Supports custom 4th-argument "DefaultValue" syntax for `Fn::FindInMap`.

## **Installation**

This project is managed with [uv](https://github.com/astral-sh/uv).

```bash
# Clone the repository
git clone git@github.com:ryanm101/samrenderer.git
cd samrenderer

# Install dependencies and sync environment
uv sync
```

## **Usage**

Run the renderer against a template file. You can optionally specify a `samconfig.toml` environment or an AWS profile.

### **Basic Rendering**

Resolves parameters using defaults defined in the template.

```bash
uv run sam-render template.yaml
```

### **Using SAM Config (Recommended)**

Applies parameters from `[<env>.deploy.parameters]` in `samconfig.toml`.

```bash
uv run sam-render template.yaml --config samconfig.toml --env dev
```

### **Comparing Environments**

Generate a colored diff between two environments defined in `samconfig.toml`. This is useful for detecting drift or verifying configuration changes between stages.

```bash
uv run sam-render template.yaml --config samconfig.toml --env dev --env2 stag
```

### **AWS Integration (Imports & Secrets)**

By default, `Fn::ImportValue` and `{{resolve:secretsmanager:...}}` return mock strings. Provide an AWS profile to fetch real values from your AWS account.

```bash
uv run sam-render template.yaml --config samconfig.toml --env dev --profile my-aws-profile
```

## **Supported Functions**

| Category | Function        | Status | Notes                                                              |
|:---------|:----------------|:-------|:-------------------------------------------------------------------|
| Core     | Ref             | ✅      | Resolves Parameters/Pseudo-params; mocks Resources.                |
|          | Fn::GetAtt      | ⚠️     | Returns mock string mock-resource-attr.                            |
|          | Fn::ImportValue | ✅      | Fetches from AWS if `--profile` is set, otherwise mocks.           |
| Logic    | Fn::If          | ✅      | Full support.                                                      |
|          | Fn::Equals      | ✅      | Full support.                                                      |
|          | Fn::Not         | ✅      | Full support.                                                      |
|          | Fn::And / Or    | ✅      | Full support.                                                      |
|          | Condition       | ✅      | Resolves Condition keys in dictionaries.                           |
| Maps     | Fn::FindInMap   | ✅      | Supports standard 3-arg and custom 4-arg (DefaultValue) syntax.    |
| String   | Fn::Sub         | ✅      | Supports String and Key-Value map interpolation.                   |
|          | Fn::Join        | ✅      | Full support.                                                      |
|          | Fn::Split       | ✅      | Full support.                                                      |
|          | Fn::Select      | ✅      | Full support.                                                      |
|          | Fn::Base64      | ⚠️     | Returns readable string `[Base64: ...]` instead of encoding.       |
|          | Fn::GetAZs      | ⚠️     | Returns mock list based on Region (e.g., us-east-1a, 1b, 1c).      |
| Dynamic  | {{resolve:...}} | ✅      | Supports Secrets Manager lookups (JSON & String) with `--profile`. |

## **Development & Testing**

Makefile is used to provide consistency between local and remote builds.

```bash
make help
```
Tests are written using pytest.

```bash
make test
```
