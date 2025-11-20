import yaml
import boto3
import re
import sys
import argparse
import difflib
import json
import asyncio
import subprocess
from botocore.exceptions import ClientError, BotoCoreError

try:
    import tomllib as toml  # Python 3.11+
except ImportError:  # pragma: no cover
    import tomli as toml  # pip install tomli

# Constants for log levels
LOG_LEVELS = {
    "DEBUG": 10,
    "INFO": 20,
    "WARN": 30,
    "WARNING": 30,
    "ERROR": 40,
    "CRITICAL": 50,
}


# --- 1. YAML Tag Handling ---
class CFNLoader(yaml.SafeLoader):
    pass


def multi_constructor(loader, tag_suffix, node):
    tag = tag_suffix

    if tag == "GetAtt":
        val = (
            loader.construct_sequence(node)
            if isinstance(node, yaml.SequenceNode)
            else loader.construct_scalar(node).split(".")
        )
        return {"Fn::GetAtt": val}
    elif isinstance(node, yaml.ScalarNode):
        val = loader.construct_scalar(node)
        key = "Ref" if tag == "Ref" else f"Fn::{tag}"
        return {key: val}
    elif isinstance(node, yaml.SequenceNode):
        return {f"Fn::{tag}": loader.construct_sequence(node)}
    elif isinstance(node, yaml.MappingNode):
        return {f"Fn::{tag}": loader.construct_mapping(node)}

    return None


CFNLoader.add_multi_constructor("!", multi_constructor)


# --- 2. SAM Config Parsing ---
def parse_sam_overrides(override_string):
    if not override_string:
        return {}

    pattern = re.compile(r"([a-zA-Z0-9\-_]+)=(?:\"([^\"]*)\"|([^\s\"]+))")

    matches = pattern.findall(override_string)

    return {m[0]: m[1] or m[2] for m in matches}


def load_sam_config(config_path, environment="default"):
    try:
        with open(config_path, "rb") as f:
            data = toml.load(f)

        params = data.get(environment, {}).get("deploy", {}).get("parameters", {})
        overrides_str = params.get("parameter_overrides", "")

        config_values = parse_sam_overrides(overrides_str)

        if "region" in params:
            config_values["AWS::Region"] = params["region"]

        return config_values

    except FileNotFoundError:
        print(f"Warning: SAM Config file '{config_path}' not found.", file=sys.stderr)
        return {}
    except Exception as e:
        print(f"Warning: Error parsing SAM Config: {e}", file=sys.stderr)
        return {}


# --- 3. AWS Login Helper ---
def ensure_sso_login(profile):
    """
    Checks if the session for the given profile is valid.
    If not, triggers 'aws sso login'.
    """
    if not profile:
        return

    print(f"Checking credentials for profile '{profile}'...", file=sys.stderr)
    try:
        session = boto3.Session(profile_name=profile)
        sts = session.client("sts")
        sts.get_caller_identity()
    except (ClientError, BotoCoreError):
        print(
            f"Credentials expired/invalid for '{profile}'. Running 'aws sso login'...",
            file=sys.stderr,
        )
        try:
            subprocess.check_call(["aws", "sso", "login", "--profile", profile])
            print(f"Login successful for '{profile}'.", file=sys.stderr)
        except subprocess.CalledProcessError:
            print(
                f"Error: Failed to login to AWS SSO for profile '{profile}'.",
                file=sys.stderr,
            )
            # We don't exit here; we let the renderer fail naturally later if it needs creds


# --- 4. Resolution Logic ---
class TemplateRenderer:
    def __init__(
        self,
        template_path,
        profile=None,
        region="us-east-1",
        env_name="default",
        log_level="WARN",
    ):
        with open(template_path, "r") as f:
            self.t = yaml.load(f, Loader=CFNLoader)

        self.mappings = self.t.get("Mappings", {})
        self.conditions = self.t.get("Conditions", {})
        self.resources = self.t.get("Resources", {})
        self.env_name = env_name
        self.profile = profile
        self.log_level_int = LOG_LEVELS.get(log_level.upper(), 30)

        # Track current resource context for logging
        self.current_resource_id = None
        self.current_resource_type = None

        self.context = {
            "AWS::Region": region,
            "AWS::AccountId": "123456789012",
            "AWS::StackName": "Local-Render-Stack",
            "AWS::Partition": "aws",
            "AWS::URLSuffix": "amazonaws.com",
            "AWS::NoValue": None,
        }

        for name, p in self.t.get("Parameters", {}).items():
            if "Default" in p:
                self.context[name] = p["Default"]

        # Initialize clients directly (assumes login handled externally)
        self.boto_session = (
            boto3.Session(profile_name=self.profile, region_name=region)
            if self.profile
            else None
        )
        self.cfn_client = (
            self.boto_session.client("cloudformation") if self.boto_session else None
        )
        self.sm_client = (
            self.boto_session.client("secretsmanager") if self.boto_session else None
        )

    def _log(self, operation, key, message=None, level="INFO"):
        msg_level_int = LOG_LEVELS.get(level.upper(), 20)
        if msg_level_int < self.log_level_int:
            return

        entry = {
            "level": level,
            "operation": operation,
            "env": self.env_name,
            "profile": self.profile,
            "key": key,
        }

        if self.current_resource_id:
            entry["resource_id"] = self.current_resource_id
        if self.current_resource_type:
            entry["resource_type"] = self.current_resource_type

        if message:
            entry["message"] = message
        print(json.dumps(entry), file=sys.stderr)

    def resolve_resources(self):
        """Special resolver for the Resources block to track context."""
        resolved = {}
        for logical_id, res_def in self.resources.items():
            self.current_resource_id = logical_id
            self.current_resource_type = (
                res_def.get("Type") if isinstance(res_def, dict) else None
            )

            # Resolve the resource definition
            resolved_val = self.resolve(res_def)

            if resolved_val is not None:
                resolved[logical_id] = resolved_val

        # Reset context
        self.current_resource_id = None
        self.current_resource_type = None
        return resolved

    def resolve(self, node):
        if isinstance(node, dict):
            if len(node) == 1:
                key = list(node.keys())[0]
                val = node[key]

                # Core
                if key == "Ref":
                    return self._handle_ref(val)
                if key == "Fn::FindInMap":
                    return self._handle_map(val)
                if key == "Fn::Sub":
                    return self._handle_sub(val)
                if key == "Fn::ImportValue":
                    return self._handle_import(val)
                if key == "Fn::Join":
                    return self._handle_join(val)
                if key == "Fn::GetAtt":
                    return self._handle_getatt(val)
                if key == "Fn::Select":
                    return self._handle_select(val)
                if key == "Fn::Split":
                    return self._handle_split(val)
                if key == "Fn::Base64":
                    return self._handle_base64(val)
                if key == "Fn::GetAZs":
                    return self._handle_getazs(val)
                if key == "Fn::Length":
                    return self._handle_length(val)

                # Logic
                if key == "Fn::If":
                    return self._handle_if(val)
                if key == "Fn::Equals":
                    return self._handle_equals(val)
                if key == "Fn::Not":
                    return self._handle_not(val)
                if key == "Fn::And":
                    return self._handle_and(val)
                if key == "Fn::Or":
                    return self._handle_or(val)
                if key == "Condition":
                    return self._handle_condition(val)
                if key == "Fn::Condition":
                    return self._handle_condition(val)

            # Filter out AWS::NoValue (None) from dictionaries
            resolved_dict = {}
            for k, v in node.items():
                resolved_v = self.resolve(v)
                if resolved_v is not None:
                    resolved_dict[k] = resolved_v
            return resolved_dict

        elif isinstance(node, list):
            # Filter out AWS::NoValue (None) from lists
            return [r for x in node if (r := self.resolve(x)) is not None]

        elif isinstance(node, str):
            # Check for CloudFormation dynamic references
            return self._resolve_dynamic_reference(node)

        return node

    # --- Intrinsic Handlers ---

    def _handle_ref(self, ref_key):
        if ref_key in self.context:
            result = self.context[ref_key]
            if isinstance(result, str):
                return self._resolve_dynamic_reference(result)
            return result
        if ref_key in self.resources:
            return f"mock-{ref_key.lower()}-id"
        return f"{{Ref: {ref_key}}}"

    def _resolve_dynamic_reference(self, text):
        """Resolve CloudFormation dynamic references like {{resolve:secretsmanager:...}}"""
        if not isinstance(text, str):
            return text

        pattern = r"\{\{resolve:([^:]+):([^}]+)\}\}"
        match = re.search(pattern, text)

        if not match:
            return text

        service = match.group(1)
        reference = match.group(2)

        if service == "secretsmanager":
            return self._resolve_secretsmanager(reference)

        return text

    def _resolve_secretsmanager(self, reference):
        """Resolve a Secrets Manager reference."""
        parts = reference.split(":")
        secret_id = parts[0]
        json_key = parts[1] if len(parts) > 1 else None

        if self.boto_session:
            try:
                sm_client = self.boto_session.client("secretsmanager")
                response = sm_client.get_secret_value(SecretId=secret_id)

                if "SecretBinary" in response:
                    self._log("Resolve:SecretsManager", reference, level="INFO")
                    return str(response["SecretBinary"])

                secret_string = response.get("SecretString", "")

                if json_key:
                    try:
                        import json

                        secret_data = json.loads(secret_string)
                        if json_key not in secret_data:
                            self._log(
                                "Resolve:SecretsManager",
                                reference,
                                f"Key {json_key} not found",
                                level="ERROR",
                            )
                            return f"{{Error: Key {json_key} not found in secret {secret_id}}}"
                        self._log("Resolve:SecretsManager", reference, level="INFO")
                        return secret_data[json_key]
                    except json.JSONDecodeError:
                        self._log(
                            "Resolve:SecretsManager",
                            reference,
                            "Invalid JSON",
                            level="ERROR",
                        )
                        return f"{{Error: Secret is not valid JSON: {secret_id}}}"

                self._log("Resolve:SecretsManager", reference, level="INFO")
                return secret_string

            except (ClientError, BotoCoreError) as e:
                self._log("Resolve:SecretsManager", reference, str(e), level="ERROR")
                pass

        return f"mock-secret-{secret_id}"

    def _handle_map(self, args):
        m_name = self.resolve(args[0])
        top = self.resolve(args[1])
        sec = self.resolve(args[2])

        key_str = f"{m_name}.{top}.{sec}"
        try:
            val = self.mappings[m_name][top][sec]
            self._log("FindInMap", key_str, level="INFO")
            return self.resolve(val)
        except (KeyError, TypeError):
            if len(args) > 3:
                default_arg = args[3]
                if isinstance(default_arg, dict) and "DefaultValue" in default_arg:
                    self._log("FindInMap", key_str, "Used Default Value", level="WARN")
                    return self.resolve(default_arg["DefaultValue"])
                self._log("FindInMap", key_str, "Used Default Value", level="WARN")
                return self.resolve(default_arg)

            self._log("FindInMap", key_str, "Key not found", level="ERROR")
            return f"{{Error: Could not resolve Map {m_name}.{top}.{sec}}}"

    def _handle_sub(self, args):
        text = args[0] if isinstance(args, list) else args
        vars_map = args[1] if isinstance(args, list) else {}

        def repl(match):
            var = match.group(1)
            if var in vars_map:
                val = str(self.resolve(vars_map[var]))
                self._log("Sub", var, f"Resolved to: {val}", level="DEBUG")
                return val
            if var in self.context:
                val = str(self.resolve(self.context[var]))
                self._log("Sub", var, f"Resolved to: {val}", level="DEBUG")
                return val
            if var in self.resources:
                val = f"mock-{var.lower()}-id"
                self._log("Sub", var, f"Resolved to Mock: {val}", level="DEBUG")
                return val
            return match.group(0)

        return re.sub(r"\${([^!][^}]*)}", repl, text)

    def _handle_import(self, val):
        import_name = self.resolve(val)
        if self.cfn_client:
            try:
                exports = self.cfn_client.list_exports()
                for exp in exports["Exports"]:
                    if exp["Name"] == import_name:
                        self._log("ImportValue", import_name, level="INFO")
                        return exp["Value"]
            except (ClientError, BotoCoreError) as e:
                self._log("ImportValue", import_name, str(e), level="ERROR")
                pass
        return f"mock-import-{import_name}"

    def _handle_getatt(self, args):
        if isinstance(args, str):
            args = args.split(".")
        res = self.resolve(args[0])
        attr = self.resolve(args[1])
        return f"mock-{res}-{attr}".lower()

    def _handle_join(self, args):
        delimiter = args[0]
        values = self.resolve(args[1])
        return delimiter.join(str(v) for v in values)

    def _handle_select(self, args):
        # [Index, List]
        idx = int(self.resolve(args[0]))
        lst = self.resolve(args[1])
        if isinstance(lst, list) and 0 <= idx < len(lst):
            return lst[idx]
        return f"{{Error: Select index {idx} out of bounds}}"

    def _handle_split(self, args):
        # [Delimiter, String]
        delim = self.resolve(args[0])
        string = self.resolve(args[1])
        return string.split(delim)

    def _handle_base64(self, val):
        resolved = self.resolve(val)
        return f"[Base64: {resolved}]"

    def _handle_length(self, val):
        item = self.resolve(val)
        return len(item) if isinstance(item, list) else 0

    def _handle_getazs(self, val):
        region = self.resolve(val)
        if not region:
            region = self.context["AWS::Region"]
        return [f"{region}a", f"{region}b", f"{region}c"]

    # --- Logic Handlers ---
    def _handle_equals(self, args):
        return self.resolve(args[0]) == self.resolve(args[1])

    def _handle_not(self, args):
        condition = args[0] if isinstance(args, list) else args
        return not self.resolve(condition)

    def _handle_and(self, args):
        return all(self.resolve(arg) for arg in args)

    def _handle_or(self, args):
        return any(self.resolve(arg) for arg in args)

    def _handle_condition(self, name):
        if name in self.conditions:
            return self.resolve(self.conditions[name])
        return False

    def _handle_if(self, args):
        condition_name = args[0]
        value_if_true = args[1]
        value_if_false = args[2]

        is_true = False
        if condition_name in self.conditions:
            is_true = self.resolve(self.conditions[condition_name])

        result_node = value_if_true if is_true else value_if_false
        return self.resolve(result_node)


# --- Processing Logic ---
def process(config, env, template, profile, log_level="WARN"):
    sam_params = load_sam_config(config, env)
    region = sam_params.get("AWS::Region", "us-east-1")

    renderer = TemplateRenderer(
        template, profile=profile, region=region, env_name=env, log_level=log_level
    )
    renderer.context.update(sam_params)

    # Use resolve_resources to track Logical ID context
    resolved_resources = renderer.resolve_resources()

    output = {
        "Resources": resolved_resources,
        "Conditions": renderer.resolve(renderer.conditions),
    }
    return output


def compare(a, b):
    a_lines = yaml.dump(a[1], sort_keys=True).splitlines()
    b_lines = yaml.dump(b[1], sort_keys=True).splitlines()

    diff = difflib.unified_diff(
        a_lines,
        b_lines,
        fromfile=f"Environment {a[0]}",
        tofile=f"Environment {b[0]}",
        lineterm="",
    )

    RED = "\033[31m"
    GREEN = "\033[32m"
    CYAN = "\033[36m"
    RESET = "\033[0m"

    colored_output = []
    for line in diff:
        if line.startswith("---") or line.startswith("+++"):
            colored_output.append(f"{CYAN}{line}{RESET}")
        elif line.startswith("-"):
            colored_output.append(f"{RED}{line}{RESET}")
        elif line.startswith("+"):
            colored_output.append(f"{GREEN}{line}{RESET}")
        elif line.startswith("@@"):
            colored_output.append(f"{CYAN}{line}{RESET}")
        else:
            colored_output.append(line)

    return "\n".join(colored_output)


async def async_main():
    parser = argparse.ArgumentParser(
        description="Render CloudFormation/SAM templates.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
      # Basic render of 'dev' environment
      sam-render template.yaml --config samconfig.toml --env dev

      # Render with AWS profile for real value lookups
      sam-render template.yaml --env dev --profile my-profile

      # Compare 'dev' and 'stag' environments (Colored Diff)
      sam-render template.yaml --env dev --env2 stag
    """,
    )
    parser.add_argument("template", help="Path to template.yaml")
    parser.add_argument(
        "--config", help="Path to samconfig.toml", default="samconfig.toml"
    )
    parser.add_argument(
        "--env", help="Environment name in samconfig (e.g., dev)", default="default"
    )
    parser.add_argument(
        "--env2",
        help="Second Environment name in samconfig (e.g., stag), used to diff the first environment against.",
        default=None,
    )
    parser.add_argument("--profile", help="AWS CLI Profile", default=None)
    parser.add_argument(
        "--profile2",
        help="AWS CLI Profile for the second environment (optional). Defaults to --profile.",
        default=None,
    )
    parser.add_argument(
        "--log-level",
        help="Set logging level (DEBUG, INFO, WARN, ERROR)",
        default="WARN",
        type=str.upper,
        choices=["DEBUG", "INFO", "WARN", "WARNING", "ERROR"],
    )

    args = parser.parse_args()

    # Determine profiles for both envs
    prof1 = args.profile
    prof2 = args.profile2 if args.profile2 else args.profile

    # Check login for any profile that is set
    profiles_to_check = set(p for p in [prof1, prof2] if p)
    for p in profiles_to_check:
        ensure_sso_login(p)

    if args.env2 is not None:
        # Run both process calls in parallel threads
        task1 = asyncio.to_thread(
            process, args.config, args.env, args.template, prof1, args.log_level
        )
        task2 = asyncio.to_thread(
            process, args.config, args.env2, args.template, prof2, args.log_level
        )

        output1, output2 = await asyncio.gather(task1, task2)

        diff = compare([args.env, output1], [args.env2, output2])
        print(diff)
    else:
        output = await asyncio.to_thread(
            process, args.config, args.env, args.template, prof1, args.log_level
        )
        print(yaml.dump(output))


def main():
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
