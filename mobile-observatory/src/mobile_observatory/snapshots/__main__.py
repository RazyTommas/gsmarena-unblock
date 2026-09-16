from __future__ import annotations

import argparse
import json
from pathlib import Path

from .bundle import SnapshotBuilder, verify_bundle


def main() -> None:
    parser = argparse.ArgumentParser(description="Build or verify an offline Mobile Observatory bundle")
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build", help="build and verify a new bundle")
    build.add_argument("--corpus", required=True, type=Path)
    build.add_argument("--output", required=True, type=Path)
    build.add_argument("--web", default="apps/web", type=Path)
    verify = sub.add_parser("verify", help="verify an existing bundle")
    verify.add_argument("bundle", type=Path)
    args = parser.parse_args()

    if args.command == "build":
        result = SnapshotBuilder(args.corpus, args.web).build(args.output)
    else:
        result = verify_bundle(args.bundle)
    print(json.dumps(result, indent=2, sort_keys=True, default=str))


if __name__ == "__main__":
    main()
