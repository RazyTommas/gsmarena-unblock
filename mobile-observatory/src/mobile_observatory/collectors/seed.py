from __future__ import annotations

import argparse
import json
from pathlib import Path

from .adapters import FixtureCatalogAdapter
from .pipeline import CollectorPipeline


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create replayable sample staging observations")
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--run-id", default="seed-supported-catalog-v1")
    args = parser.parse_args(argv)
    result = CollectorPipeline(args.output).run(FixtureCatalogAdapter(args.fixture), args.run_id)
    print(json.dumps(result.run.as_dict(), indent=2, sort_keys=True))
    return 0 if result.run.state == "healthy" else 1


if __name__ == "__main__":
    raise SystemExit(main())
