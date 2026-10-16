
"""Convert OpenAPI operations into Freya api_call tools."""

import json
from pathlib import Path
from copy import deepcopy


def load_openapi(path):
    """Load and validate the minimum OpenAPI structure we need."""
    with Path(path).open("r", encoding="utf-8") as f:
        spec = json.load(f)

    if not isinstance(spec.get("paths"), dict):
        raise ValueError("OpenAPI document must contain a paths object.")

    servers = spec.get("servers", [])
    if not servers or not servers[0].get("url"):
        raise ValueError("OpenAPI document must define a server URL.")

    return spec


def resolve_local_ref(spec, schema):
    """Resolve simple local component references when present."""
    schema = deepcopy(schema)

    while isinstance(schema, dict) and "$ref" in schema:
        ref = schema.pop("$ref")
        if not ref.startswith("#/"):
            raise ValueError(f"Unsupported external reference: {ref}")

        target = spec
        for part in ref[2:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            target = target[part]

        merged = deepcopy(target)
        merged.update(schema)
        schema = merged

    return schema


def tool_schema(spec, schema, variable_name):
    """Translate an OpenAPI property into a Freya-compatible schema."""
    schema = resolve_local_ref(spec, schema)

    result = {}
    for key in ("type", "description", "enum", "format",
                "items", "properties", "required", "nullable"):
        if key in schema:
            value = schema[key]

            if key == "items":
                value = tool_schema(spec, value, variable_name + "_item")
            elif key == "properties":
                value = {
                    name: tool_schema(spec, child, name)
                    for name, child in value.items()
                }

            result[key] = deepcopy(value)

    # The model supplies this value at call time.
    # customer_id is injected from the Freya call variable.
    if schema.get("type") in ("string", "number", "integer", "boolean",
                              "object", "array"):
        result["default"] = "{{" + variable_name + "}}"

    return result


def operation_to_tool(spec, path, method, operation):
    """Convert one OpenAPI operation into a Freya HTTP tool."""
    server_url = spec["servers"][0]["url"].rstrip("/")
    endpoint = server_url + "/" + path.lstrip("/")
    method = method.upper()

    request_body = operation.get("requestBody", {})
    content = request_body.get("content", {})
    json_content = content.get("application/json", {})
    body_schema = resolve_local_ref(
        spec, json_content.get("schema", {"type": "object"})
    )

    body_properties = body_schema.get("properties", {})
    body_required = body_schema.get("required", [])

    properties = {}
    for name, schema in body_properties.items():
        if name == "customer_id":
            prop = resolve_local_ref(spec, schema)
            prop["default"] = "{{customer_id}}"
            properties[name] = prop
        else:
            properties[name] = tool_schema(spec, schema, name)

    description = (
        operation.get("summary")
        or operation.get("description")
        or f"{method} {path}"
    )

    name = operation.get("operationId")
    if not name:
        name = path.strip("/").replace("/", "_") or "root"

    return {
        "type": "function",
        "config": {
            "timeoutMs": 10000,
            "maxRetries": 0,
        },
        "function": {
            "name": name,
            "handler": "__function__:api_call",
            "description": description,
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "default": endpoint,
                    },
                    "method": {
                        "type": "string",
                        "default": method,
                    },
                    "headers": {
                        "type": "object",
                        "properties": {
                            "Content-Type": {
                                "type": "string",
                                "default": "application/json",
                            }
                        },
                    },
                    "body": {
                        "type": "object",
                        "properties": properties,
                        "required": list(body_required),
                    },
                },
            },
        },
    }


def parse_tools(spec):
    """Build tools for all supported HTTP operations."""
    tools = []

    for path, path_item in spec["paths"].items():
        for method, operation in path_item.items():
            if method.lower() not in {
                "get", "post", "put", "patch", "delete"
            }:
                continue

            if not isinstance(operation, dict):
                continue

            # This initial compiler supports JSON request bodies.
            if "requestBody" not in operation:
                continue

            tools.append(
                operation_to_tool(spec, path, method, operation)
            )

    names = [tool["function"]["name"] for tool in tools]
    if len(names) != len(set(names)):
        raise ValueError("OpenAPI contains duplicate generated tool names.")

    return tools


def load_tools(path):
    return parse_tools(load_openapi(path))


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("openapi", help="Path to openapi.json")
    parser.add_argument("--output", help="Optional output JSON file")
    args = parser.parse_args()

    tools = load_tools(args.openapi)
    result = json.dumps(tools, indent=2, ensure_ascii=False)

    if args.output:
        Path(args.output).write_text(result + "\n", encoding="utf-8")
    else:
        print(result)

    print(f"\nGenerated {len(tools)} tools.", file=__import__("sys").stderr)