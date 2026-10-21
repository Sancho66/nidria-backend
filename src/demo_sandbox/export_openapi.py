"""Generate the isolated entry's contract without connecting to any service."""

import json

from src.demo_sandbox.application import app
from src.demo_sandbox.catalog import CATALOG_PATH

if __name__ == "__main__":
    target = CATALOG_PATH.with_name("openapi.json")
    target.write_text(json.dumps(app.openapi(), indent=2, ensure_ascii=False) + "\n")
