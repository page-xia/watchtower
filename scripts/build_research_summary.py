from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.research_artifacts import build_compact_research_report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build the compact API sidecar for an existing research report."
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=Path("data/runtime/strategy-research/latest.json"),
    )
    parser.add_argument(
        "--target",
        type=Path,
        default=Path("data/runtime/strategy-research/latest_summary.json"),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    with args.source.open("r", encoding="utf-8") as handle:
        report = json.load(handle)
    summary = build_compact_research_report(report)
    args.target.parent.mkdir(parents=True, exist_ok=True)
    args.target.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    protocol = summary.get("research_protocol") or {}
    print(
        json.dumps(
            {
                "source": str(args.source),
                "target": str(args.target),
                "run_id": protocol.get("run_id", ""),
                "validation_status": (protocol.get("validation") or {}).get("status", ""),
                "bytes": args.target.stat().st_size,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
