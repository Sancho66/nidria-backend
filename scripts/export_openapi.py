"""Regenerate the committed openapi.json from the FastAPI app.

Contract-first rule (CLAUDE.md PARTIE 3): run after every API change.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.main import app  # noqa: E402


def main() -> None:
    spec = app.openapi()
    path = Path(__file__).resolve().parent.parent / "openapi.json"
    with path.open("w", encoding="utf-8") as f:
        json.dump(spec, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(f"openapi.json regenerated ({len(spec['paths'])} paths).")


if __name__ == "__main__":
    main()
