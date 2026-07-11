"""Regenerate the committed openapi.json from the FastAPI app.

Contract-first rule (CLAUDE.md PARTIE 3): run after every API change.
"""

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.main import app  # noqa: E402

# Keys whose presence makes a schema meaningful even without `properties`.
_SHAPE_KEYS = (
    "properties",
    "enum",
    "$ref",
    "allOf",
    "anyOf",
    "oneOf",
    "additionalProperties",
    "items",
    "type",
)


def assert_no_empty_schema(spec: dict[str, Any]) -> None:
    """Health gate on the generated contract: a NAMED component schema can
    never be empty. Pydantic exports {} (or a bare title) when something
    blinds the generation — a @model_serializer with an opaque return type
    did exactly that to CaseDetailResponse (2026-07-11 front bug), and the
    commit-vs-generated CI diff cannot see it: both sides are equally empty.
    This check fails the EXPORT itself, so the regen (and the CI openapi
    job that runs it) goes red instead of shipping a hollow contract."""
    empty = [
        name
        for name, schema in spec.get("components", {}).get("schemas", {}).items()
        if not any(key in schema for key in _SHAPE_KEYS)
    ]
    if empty:
        raise SystemExit(
            f"openapi export produced EMPTY schema(s): {', '.join(sorted(empty))} — "
            "a named model exported without shape (check for @model_serializer "
            "or config blinding the JSON-schema generation)."
        )


def main() -> None:
    spec = app.openapi()
    assert_no_empty_schema(spec)
    path = Path(__file__).resolve().parent.parent / "openapi.json"
    with path.open("w", encoding="utf-8") as f:
        json.dump(spec, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(f"openapi.json regenerated ({len(spec['paths'])} paths).")


if __name__ == "__main__":
    main()
