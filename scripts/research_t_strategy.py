from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

# Allow ``python scripts/research_t_strategy.py`` from the repository root.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.strategy_research import StrategyResearcher, ResearchConfig  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="用真实 easy_tdx 分钟与历史逐笔成交做日内 T 策略事件研究")
    parser.add_argument(
        "--dates",
        default="20260806,20260807",
        help="目标交易日，逗号分隔；默认使用已有的两天真实分钟缓存",
    )
    parser.add_argument("--start", default="", help="研究起始交易日YYYYMMDD；与--end配合使用")
    parser.add_argument("--end", default="", help="研究结束交易日YYYYMMDD")
    parser.add_argument("--lookback-days", type=int, default=0, help="从缓存/交易日历向前取N个交易日")
    parser.add_argument("--selection-date", default="20260805", help="样本选择日，只用于事前流动性排序")
    parser.add_argument("--sample-size", type=int, default=100, help="跨行业非一字板样本数量，默认100")
    parser.add_argument("--no-transactions", action="store_true", help="只用分钟价格/成交额代理，不拉历史逐笔成交")
    parser.add_argument("--protocol", default="research_first", choices=("research_first", "legacy"), help="研究协议；默认先跑研究优先流程")
    parser.add_argument("--json-summary", action="store_true", help="只打印一行JSON摘要")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    dates = _resolve_dates(args)
    if not dates:
        raise SystemExit("--dates 不能为空")
    config = ResearchConfig(
        dates=dates,
        selection_date=str(args.selection_date),
        sample_size=max(20, min(int(args.sample_size), 100)),
        include_transactions=not args.no_transactions,
        protocol=str(args.protocol),
    )
    report = StrategyResearcher(config=config).run()
    formula_grid_value = report.get("formula_grid", {})
    formula_grid = formula_grid_value if isinstance(formula_grid_value, dict) else {}
    formula_grid_rows = formula_grid.get("rows", [])
    sample_sufficiency = formula_grid.get("sample_sufficiency", {})
    payload = {
        "sample": report.get("sample", {}).get("count", 0),
        "dates": [item.get("date") for item in report.get("date_summaries", [])],
        "flow_mode": report.get("data_quality", {}).get("flow_mode"),
        "formula_grid": {
            "status": formula_grid.get("status", "research_only"),
            "validation_status": formula_grid.get(
                "validation_status",
                "sample_insufficient",
            ),
            "thresholds_pct": formula_grid.get("thresholds_pct", []),
            "outcome_horizons_minutes": formula_grid.get(
                "outcome_horizons_minutes",
                [],
            ),
            "l1_rules": [
                item.get("key")
                for item in formula_grid.get("l1_rules", [])
                if isinstance(item, dict)
            ],
            "rows": len(formula_grid_rows),
            "formula_ready_count": formula_grid.get("formula_ready_count", 0),
            "formula_candidate_union_count": formula_grid.get(
                "formula_candidate_union_count",
                0,
            ),
            "missing_formula_fields": formula_grid.get("missing_formula_fields", 0),
            "l1_indicator_ready_count": formula_grid.get(
                "l1_indicator_ready_count",
                0,
            ),
            "l1_available_count": formula_grid.get("l1_available_count", 0),
            "sample_sufficiency": sample_sufficiency,
            "selected_threshold_pct": formula_grid.get("selected_threshold_pct"),
            "selected_l1_rule": formula_grid.get("selected_l1_rule"),
            "selection": formula_grid.get("selection", {"selected": False}),
            "plateau_count": len(formula_grid.get("plateaus", [])),
        },
        "research_status": report.get("research_protocol", {}).get("validation", {}).get("status", "research_only"),
        "research_protocol": {
            "candidates": len(report.get("research_protocol", {}).get("candidates", [])),
            "labels": len(report.get("research_protocol", {}).get("labels", [])),
            "variants": list(report.get("research_protocol", {}).get("variants", {}).keys()),
        },
        "excluded_one_word": report.get("sample", {}).get("excluded_one_word_codes", []),
        "report": str(
            Path(
                "data/runtime/strategy-research/"
                f"latest_{'proxy' if args.no_transactions else 'transactions'}.md"
            )
        ),
    }
    if args.json_summary:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        print("详细报告：data/runtime/strategy-research/latest.md")
        print("原始 JSON：data/runtime/strategy-research/latest.json")
    return 0


def _resolve_dates(args: argparse.Namespace) -> tuple[str, ...]:
    """Resolve explicit/range/lookback dates without hard-coded study days."""

    explicit = tuple(item.strip() for item in str(args.dates).split(",") if item.strip())
    if not (args.start or args.end or args.lookback_days):
        return explicit
    runtime = ROOT / "data" / "runtime"
    available: set[str] = set()
    for path in runtime.glob("daily_*.json"):
        value = path.stem.removeprefix("daily_")
        if len(value) == 8 and value.isdigit():
            available.add(value)
    for path in (runtime / "strategy-research" / "cache" / "minute").glob("*_index_000001.json"):
        value = path.name.split("_", 1)[0]
        if len(value) == 8 and value.isdigit():
            available.add(value)
    if not available:
        # The researcher will use its calendar adapter when the local cache is
        # incomplete; retain an explicit range rather than inventing dates.
        if args.start and args.end:
            return (str(args.start), str(args.end))
        return explicit
    lower = str(args.start or min(available))
    upper = str(args.end or max(available))
    selected = sorted(item for item in available if lower <= item <= upper)
    if args.lookback_days > 0:
        selected = selected[-int(args.lookback_days):]
    return tuple(selected or explicit)


if __name__ == "__main__":
    raise SystemExit(main())
