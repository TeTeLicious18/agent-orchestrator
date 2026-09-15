"""Export the OpenAPI contract to docs/openapi.json.

The checked-in spec is what reviewers and client generators read, so it has to be
regenerated whenever a route or model changes. Run this, commit the diff.

The environment is pinned here rather than inherited: importing the app would otherwise
pick up a developer's .env through load_dotenv and bake a live Foundry endpoint into the
exported document.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
# Running this file directly puts tools/ on sys.path, not the repository root.
sys.path.insert(0, str(ROOT))

os.environ["ORCH_API_KEYS"] = "openapi-export"
os.environ["ORCH_BOOTSTRAP_TOKEN"] = "openapi-export"  # noqa: S105 - placeholder; never used to serve traffic
os.environ["ORCH_DB_PATH"] = str(pathlib.Path(tempfile.gettempdir()) / "openapi-export.db")
os.environ["ORCH_DISABLE_SCHEDULER"] = "1"
os.environ["FOUNDRY_PROJECT_ENDPOINT"] = ""
os.environ["ORCH_FOUNDRY_ENDPOINT"] = ""
os.environ["ORCH_FOUNDRY_CATALOG_FILE"] = ""

from orchestrator.main import app  # noqa: E402 - must follow the environment pinning above

TARGET = ROOT / "docs" / "openapi.json"


def main() -> None:
    spec = app.openapi()
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(json.dumps(spec, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"docs/openapi.json: {len(spec['paths'])} paths")


if __name__ == "__main__":
    main()
