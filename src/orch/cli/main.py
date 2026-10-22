"""`orch` command line entry point (stub; commands arrive with the phase 1 tasks)."""

import sys
from collections.abc import Sequence

from orch import __version__


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args in (["--version"], ["-V"]):
        print(__version__)
        return 0
    print("orch: not implemented yet. Use `orch --version`.", file=sys.stderr)
    return 0 if not args else 2


if __name__ == "__main__":
    raise SystemExit(main())
