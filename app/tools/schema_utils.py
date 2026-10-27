"""Turn Pydantic JSON schemas into compact, self-contained function-calling schemas.

Inlines ``$ref``/``$defs`` (not every OpenAI-compatible provider resolves
references) and drops auto-generated ``title`` keys, which only cost tokens.
"""

from __future__ import annotations

import copy
from typing import Any

from pydantic import BaseModel


def clean_schema(model: type[BaseModel], drop_properties: tuple[str, ...] = ()) -> dict[str, Any]:
    schema = copy.deepcopy(model.model_json_schema())
    defs = schema.pop("$defs", {})

    def resolve(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                target = resolve(defs[node["$ref"].rsplit("/", 1)[-1]])
                extra = {k: v for k, v in node.items() if k != "$ref"}
                return {**target, **resolve(extra)}
            return {k: resolve(v) for k, v in node.items() if k != "title" or not isinstance(v, str)}
        if isinstance(node, list):
            return [resolve(x) for x in node]
        return node

    schema = resolve(schema)
    for name in drop_properties:
        schema.get("properties", {}).pop(name, None)
        if name in schema.get("required", []):
            schema["required"].remove(name)
    schema.setdefault("properties", {})
    return schema
