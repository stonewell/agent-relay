"""Build step: `python -m orch.core.specs [directory]` regenerates `schemas/*.json`."""

import sys
from pathlib import Path

from . import export_schemas

if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("schemas")
    for path in export_schemas(target):
        print(path)
