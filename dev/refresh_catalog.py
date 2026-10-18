"""Refresh tests/fixtures/catalog.json from an ops-foundation checkout (metadata only, no runbooks).

python dev/refresh_catalog.py ../ops-foundation
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import yaml

OUT = Path(__file__).resolve().parents[1] / "tests/fixtures/catalog.json"


def main() -> None:
    repo = Path(sys.argv[1] if len(sys.argv) > 1 else "../ops-foundation").resolve()
    subprocess.run([sys.executable, "tools/build_catalog.py"], cwd=repo, check=True)  # noqa: S603
    catalog = json.loads((repo / "build/catalog.json").read_text(encoding="utf-8"))
    catalog["alerts"] = [
        yaml.safe_load(p.read_text(encoding="utf-8")) for p in sorted((repo / "observability/alerts").glob("*.yml"))
    ]
    catalog["_note"] = (
        "Test fixture: snapshot of ops-foundation build/catalog.json plus its alert specs. Refresh with dev/refresh_catalog.py."
    )
    OUT.write_text(json.dumps(catalog, indent=2, default=str) + "\n", encoding="utf-8")
    print(
        f"wrote {OUT} from ops-foundation {catalog.get('commit', '')[:7]}: {len(catalog['runbooks'])} runbooks, {len(catalog['alerts'])} alerts"
    )


if __name__ == "__main__":
    main()
