"""Expose all Python APIs to the native advanced-operation form."""
from __future__ import annotations

import copy


def resolve_schema(value, components, seen=()):
    if isinstance(value, list):
        return [resolve_schema(v, components, seen) for v in value]
    if not isinstance(value, dict):
        return value
    if "$ref" in value:
        reference = value["$ref"]
        name = reference.rsplit("/", 1)[-1]
        if name in seen: return {"type": "object", "description": name}
        source = copy.deepcopy(components.get(name, {}))
        source.update({k: v for k, v in value.items() if k != "$ref"})
        return resolve_schema(source, components, (*seen, name))
    return {k: resolve_schema(v, components, seen) for k, v in value.items()}


def api_catalog(*applications):
    operations = []
    for prefix, application in applications:
        schema = application.openapi()
        components = schema.get("components", {}).get("schemas", {})
        for path, methods in schema.get("paths", {}).items():
            for method, spec in methods.items():
                if method.upper() not in {"GET", "POST", "PUT", "PATCH", "DELETE"}: continue
                if path.startswith("/api/desktop"): continue
                operations.append({
                    "method": method.upper(), "path": prefix + path,
                    "summary": spec.get("summary", ""),
                    "description": spec.get("description", ""),
                    "parameters": resolve_schema(spec.get("parameters", []), components),
                    "requestBody": resolve_schema(spec.get("requestBody", {}), components),
                })
    return {"operations": sorted(operations, key=lambda op: (op["path"], op["method"]))}
