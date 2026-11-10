"""Build and validate the app package the way Agents Toolkit does, without a tenant.

- ${{VAR}} placeholders are filled from env/.env.<env> (and .env.<env>.user if present).
- $[file('x')] is replaced by the content of x, as Agents Toolkit 5.10+ does.
- Every manifest is validated against Microsoft's official JSON schema, downloaded
  from developer.microsoft.com, plus checks the schemas can't express (plugin
  functions match OpenAPI operationIds, no nested request bodies, and so on).
"""

from __future__ import annotations

import json
import re
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

import jsonschema
import yaml

SCHEMAS = {
    "teams-manifest-1.25.json": "https://developer.microsoft.com/json-schemas/teams/v1.25/MicrosoftTeams.schema.json",
    "declarative-agent-1.9.json": "https://developer.microsoft.com/json-schemas/copilot/declarative-agent/v1.9/schema.json",
    "plugin-2.4.json": "https://developer.microsoft.com/json-schemas/copilot/plugin/v2.4/schema.json",
    "openapi-3.0.json": "https://spec.openapis.org/oas/3.0/schema/2021-09-28",
    "m365agents-1.10.json": "https://aka.ms/m365-agents-toolkits/v1.10/yaml.schema.json",
}
SPEC_PATH = "apiSpecificationFile/devassist-actions.yml"
PACKAGE_FILES = ("manifest.json", "declarativeAgent.json", "ai-plugin.json", SPEC_PATH, "color.png", "outline.png")
_PLACEHOLDER = re.compile(r"\$\{\{\s*([A-Z0-9_]+)\s*\}\}")
_FILE_FUNCTION = re.compile(r"\$\[file\('([^']+)'\)\]")
MAX_INSTRUCTIONS = 8000


def read_env(project_dir: Path, env: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for name in (f".env.{env}", f".env.{env}.user"):
        path = project_dir / "env" / name
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip() and not line.lstrip().startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip()
    if not values:
        raise ValueError(f"No env/.env.{env} file in {project_dir}.")
    return values


def render(text: str, values: dict[str, str], base: Path, json_strings: bool = False) -> tuple[str, list[str]]:
    missing: list[str] = []

    def placeholder(match: re.Match[str]) -> str:
        value = values.get(match.group(1), "")
        if not value and match.group(1) != "APP_NAME_SUFFIX":
            missing.append(match.group(1))
        return value

    def file_function(match: re.Match[str]) -> str:
        content = (base / match.group(1)).read_text(encoding="utf-8")
        return json.dumps(content)[1:-1] if json_strings else content

    return _PLACEHOLDER.sub(placeholder, _FILE_FUNCTION.sub(file_function, text)), missing


def rendered_files(project_dir: Path, env: str) -> tuple[dict[str, bytes], list[str]]:
    values = read_env(project_dir, env)
    app = project_dir / "appPackage"
    files: dict[str, bytes] = {}
    problems: list[str] = []
    for name in PACKAGE_FILES:
        path = app / name
        if not path.exists():
            problems.append(f"appPackage/{name} is missing")
            continue
        if path.suffix == ".png":
            files[name] = path.read_bytes()
            continue
        text, missing = render(path.read_text(encoding="utf-8"), values, app, json_strings=path.suffix == ".json")
        problems.extend(f"appPackage/{name}: no value for {var} in env/.env.{env}" for var in sorted(set(missing)))
        files[name] = text.encode("utf-8")
    return files, problems


def build_package(project_dir: Path, env: str, output: Path | None = None) -> Path:
    files, problems = rendered_files(project_dir, env)
    if problems:
        raise ValueError("; ".join(problems))
    output = output or project_dir / "appPackage" / "build" / f"appPackage.{env}.zip"
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return output


def load_schemas(schema_dir: Path, download: bool = True) -> dict[str, Any]:
    schemas = {}
    schema_dir.mkdir(parents=True, exist_ok=True)
    for name, url in SCHEMAS.items():
        path = schema_dir / name
        if not path.exists() and download:
            request = urllib.request.Request(url, headers={"User-Agent": "devassist-actions-validator"})
            with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - fixed Microsoft/OpenAPI URLs
                path.write_bytes(response.read())
        if path.exists():
            schemas[name] = json.loads(path.read_text(encoding="utf-8-sig"))
    return schemas


def _validator_class(schema: dict[str, Any]) -> Any:
    """Microsoft's schemas declare draft-04 but use later keywords (const, if/then), which draft-04 would ignore."""
    base = jsonschema.validators.validator_for(schema)
    if base is jsonschema.Draft4Validator:
        later = jsonschema.Draft7Validator.VALIDATORS
        return jsonschema.validators.extend(base, {k: later[k] for k in ("const", "contains", "if", "propertyNames")})
    return base


def _schema_errors(label: str, document: Any, schema: dict[str, Any]) -> list[str]:
    validator_class = _validator_class(schema)
    validator = validator_class(schema, format_checker=validator_class.FORMAT_CHECKER)
    return [f"{label}: {'/'.join(map(str, error.absolute_path)) or '(root)'}: {error.message}" for error in validator.iter_errors(document)]


def _walk(node: Any):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


def copilot_rules(manifest: dict, agent: dict, plugin: dict, spec: dict, files: dict[str, bytes]) -> list[str]:
    """Constraints from the Microsoft 365 Copilot docs that the JSON schemas don't cover."""
    problems = []
    for ref in manifest.get("copilotAgents", {}).get("declarativeAgents", []):
        if ref["file"] not in files:
            problems.append(f"manifest.json references {ref['file']}, which is not in the package")
    if len(agent.get("instructions", "")) > MAX_INSTRUCTIONS:
        problems.append(f"declarativeAgent.json: instructions are {len(agent['instructions'])} characters, over {MAX_INSTRUCTIONS}")
    for action in agent.get("actions", []):
        if action["file"] not in files:
            problems.append(f"declarativeAgent.json references {action['file']}, which is not in the package")

    operations = {}
    for path, item in spec.get("paths", {}).items():
        for method, operation in item.items():
            if method in ("get", "post", "put", "patch", "delete"):
                operations[operation.get("operationId")] = (method, path, operation)
    if None in operations:
        problems.append("OpenAPI: every operation needs an operationId")
    functions = {f["name"] for f in plugin.get("functions", [])}
    for name in functions - set(operations):
        problems.append(f"ai-plugin.json: function {name} has no matching operationId")
    for name in set(operations) - functions:
        problems.append(f"OpenAPI operation {name} is not exposed as a plugin function")
    for runtime in plugin.get("runtimes", []):
        url = runtime.get("spec", {}).get("url")
        if runtime.get("type") == "OpenApi" and url not in files:
            problems.append(f"ai-plugin.json: runtime spec {url} is not in the package")
        for name in runtime.get("run_for_functions", []):
            if name not in functions:
                problems.append(f"ai-plugin.json: run_for_functions lists unknown {name}")
    # Known unsupported OpenAPI features for API plugins.
    for node in _walk(spec):
        for keyword in ("oneOf", "anyOf", "allOf"):
            if keyword in node:
                problems.append(f"OpenAPI: {keyword} is not supported by API plugins")
    for name, (method, path, operation) in operations.items():
        schema = operation.get("requestBody", {}).get("content", {}).get("application/json", {}).get("schema", {})
        for prop, definition in schema.get("properties", {}).items():
            if definition.get("type") in ("object", "array") or "$ref" in definition:
                problems.append(f"OpenAPI {name}: request body property {prop} is nested, which API plugins don't support")
        for parameter in operation.get("parameters", []):
            if "$ref" not in parameter and parameter.get("schema", {}).get("type") in ("object", "array"):
                problems.append(f"OpenAPI {name}: parameter {parameter['name']} is nested")
        if method != "get" and not next((f for f in plugin["functions"] if f["name"] == name), {}).get("capabilities", {}).get("confirmation", {}).get("type") == "AdaptiveCard":
            problems.append(f"ai-plugin.json: {name} changes GitLab but has no confirmation card")
    servers = spec.get("servers", [])
    if not servers or not str(servers[0].get("url", "")).startswith("https://"):
        problems.append("OpenAPI: the server URL must be https://")
    return problems


def validate_project(project_dir: Path, env: str = "local", schema_dir: Path | None = None, download: bool = True) -> list[str]:
    files, problems = rendered_files(project_dir, env)
    schemas = load_schemas(schema_dir or project_dir / ".schemas", download)
    missing = sorted(set(SCHEMAS) - set(schemas))
    if missing:
        problems.append(f"Schemas not available (run without --offline once): {', '.join(missing)}")

    def parse(name: str) -> Any:
        try:
            return yaml.safe_load(files[name]) if name.endswith(".yml") else json.loads(files[name])
        except (KeyError, ValueError, yaml.YAMLError) as exc:
            problems.append(f"{name}: cannot be parsed ({exc})")
            return {}

    manifest, agent, plugin, spec = (parse(n) for n in ("manifest.json", "declarativeAgent.json", "ai-plugin.json", SPEC_PATH))
    toolkit = yaml.safe_load((project_dir / "m365agents.yml").read_text(encoding="utf-8"))
    for label, document, schema in (
        ("manifest.json", manifest, "teams-manifest-1.25.json"),
        ("declarativeAgent.json", agent, "declarative-agent-1.9.json"),
        ("ai-plugin.json", plugin, "plugin-2.4.json"),
        (SPEC_PATH, spec, "openapi-3.0.json"),
        ("m365agents.yml", toolkit, "m365agents-1.10.json"),
    ):
        if schema in schemas:
            problems.extend(_schema_errors(label, document, schemas[schema]))
    if manifest and agent and plugin and spec:
        problems.extend(copilot_rules(manifest, agent, plugin, spec, files))
    return problems
