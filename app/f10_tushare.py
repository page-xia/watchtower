"""Tushare Pro 聚合 F10 数据源：按分类输出结构化板块。

设计说明：
- 按需读取（个股详情页 F10 标签点击时才调用），带 TTL 缓存，不进任何轮询循环。
- 数据以「分类 F10Category → 板块 FundamentalSection → 字段/表格」三层结构输出，
  前端只做展示，不在浏览器端做单位换算。
- easy_tdx finance_info（股本结构）作为补充分类并入同一 payload。
"""

from __future__ import annotations

import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable

from .models import F10Category, F10Payload, FundamentalField, FundamentalSection, FundamentalTable

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 格式化
# ---------------------------------------------------------------------------


def _is_blank(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, float) and value != value:  # NaN
        return True
    if isinstance(value, str) and not value.strip():
        return True
    return False


def _fmt_amount(value: Any) -> str:
    """元 → 亿/万 可读文本。"""
    if _is_blank(value):
        return "--"
    try:
        num = float(value)
    except (TypeError, ValueError):
        return str(value)
    sign = "-" if num < 0 else ""
    num = abs(num)
    if num >= 1e8:
        return f"{sign}{num / 1e8:.2f}亿"
    if num >= 1e4:
        return f"{sign}{num / 1e4:.0f}万"
    return f"{sign}{num:.2f}"


def _fmt_shares(value: Any) -> str:
    """股 → 亿股/万股。"""
    if _is_blank(value):
        return "--"
    try:
        num = float(value)
    except (TypeError, ValueError):
        return str(value)
    sign = "-" if num < 0 else ""
    num = abs(num)
    if num >= 1e8:
        return f"{sign}{num / 1e8:.2f}亿股"
    if num >= 1e4:
        return f"{sign}{num / 1e4:.0f}万股"
    return f"{sign}{num:.0f}股"


def _fmt_pct(value: Any) -> str:
    if _is_blank(value):
        return "--"
    try:
        return f"{float(value):.2f}%"
    except (TypeError, ValueError):
        return str(value)


def _fmt_num(value: Any, digits: int = 2) -> str:
    if _is_blank(value):
        return "--"
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return str(value)


def _fmt_int(value: Any) -> str:
    if _is_blank(value):
        return "--"
    try:
        return f"{int(float(value)):,}"
    except (TypeError, ValueError):
        return str(value)


def _fmt_date(value: Any) -> str:
    if _is_blank(value):
        return "--"
    text = str(value).strip()
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    return text[:10] if len(text) > 10 else text


def _fmt_text(value: Any, max_len: int = 400) -> str:
    if _is_blank(value):
        return "--"
    text = str(value).strip()
    return text if len(text) <= max_len else f"{text[:max_len]}…"


def _fmt_change(value: Any) -> str:
    """股东持股变动：数字 → 带符号股数，文本（新进/退出等）保留。"""
    if _is_blank(value):
        return "--"
    try:
        num = float(value)
    except (TypeError, ValueError):
        return str(value)
    if num == 0:
        return "不变"
    body = _fmt_shares(abs(num))
    return f"+{body}" if num > 0 else f"-{body}"


_FORMATTERS: dict[str, Callable[[Any], str]] = {
    "amount": _fmt_amount,
    "shares": _fmt_shares,
    "pct": _fmt_pct,
    "num": _fmt_num,
    "num3": lambda v: _fmt_num(v, 3),
    "int": _fmt_int,
    "change": _fmt_change,
    "date": _fmt_date,
    "text": _fmt_text,
}


def _fmt(value: Any, kind: str) -> str:
    return _FORMATTERS.get(kind, _fmt_text)(value)


# ---------------------------------------------------------------------------
# 字段/表格规格
# ---------------------------------------------------------------------------

# (raw_key, 中文标签, 格式)
COMPANY_BASIC_SPEC = [
    ("name", "简称", "text"),
    ("fullname", "公司全称", "text"),
    ("enname", "英文名称", "text"),
    ("industry", "所属行业", "text"),
    ("market", "市场", "text"),
    ("exchange", "交易所", "text"),
    ("list_date", "上市日期", "date"),
    ("list_status", "上市状态", "text"),
    ("is_hs", "沪深港通", "text"),
    ("act_name", "实控人", "text"),
    ("act_ent_type", "实控人类型", "text"),
    ("curr_type", "交易货币", "text"),
]

COMPANY_ORG_SPEC = [
    ("chairman", "董事长", "text"),
    ("manager", "总经理", "text"),
    ("secretary", "董秘", "text"),
    ("reg_capital", "注册资本(万元)", "num"),
    ("setup_date", "成立日期", "date"),
    ("province", "省份", "text"),
    ("city", "城市", "text"),
    ("employees", "员工人数", "int"),
    ("website", "官网", "text"),
    ("email", "邮箱", "text"),
    ("office", "办公地址", "text"),
]

COMPANY_TEXT_SPEC = [
    ("introduction", "公司简介"),
    ("main_business", "主营业务"),
    ("business_scope", "经营范围"),
]

INDICATOR_SPEC = [
    ("eps", "基本EPS(元)", "num3"),
    ("dt_eps", "稀释EPS(元)", "num3"),
    ("bps", "每股净资产(元)", "num"),
    ("ocfps", "每股经营现金流(元)", "num"),
    ("roe", "ROE", "pct"),
    ("roe_dt", "扣非ROE", "pct"),
    ("grossprofit_margin", "毛利率", "pct"),
    ("gross_margin", "毛利额", "amount"),
    ("netprofit_margin", "净利率", "pct"),
    ("debt_to_assets", "资产负债率", "pct"),
    ("current_ratio", "流动比率", "num"),
    ("quick_ratio", "速动比率", "num"),
    ("or_yoy", "营收同比", "pct"),
    ("netprofit_yoy", "净利润同比", "pct"),
    ("dt_netprofit_yoy", "扣非净利同比", "pct"),
    ("roa", "总资产净利率", "pct"),
    ("assets_turn", "总资产周转率(次)", "num"),
]

INCOME_SPEC = [
    ("total_revenue", "营业总收入", "amount"),
    ("revenue", "营业收入", "amount"),
    ("oper_cost", "营业成本", "amount"),
    ("sell_exp", "销售费用", "amount"),
    ("admin_exp", "管理费用", "amount"),
    ("rd_exp", "研发费用", "amount"),
    ("fin_exp", "财务费用", "amount"),
    ("operate_profit", "营业利润", "amount"),
    ("total_profit", "利润总额", "amount"),
    ("n_income", "净利润", "amount"),
    ("n_income_attr_p", "归母净利润", "amount"),
    ("basic_eps", "基本每股收益(元)", "num3"),
    ("ebit", "EBIT", "amount"),
    ("ebitda", "EBITDA", "amount"),
]

BALANCE_SPEC = [
    ("total_assets", "资产总计", "amount"),
    ("total_liab", "负债合计", "amount"),
    ("total_hldr_eqy_exc_min_int", "归母股东权益", "amount"),
    ("money_cap", "货币资金", "amount"),
    ("trad_asset", "交易性金融资产", "amount"),
    ("accounts_receiv", "应收账款", "amount"),
    ("inventories", "存货", "amount"),
    ("goodwill", "商誉", "amount"),
    ("total_cur_assets", "流动资产合计", "amount"),
    ("total_cur_liab", "流动负债合计", "amount"),
    ("st_borr", "短期借款", "amount"),
    ("lt_borr", "长期借款", "amount"),
    ("total_share", "期末总股本", "shares"),
]

CASHFLOW_SPEC = [
    ("c_fr_sale_sg", "销售商品收到现金", "amount"),
    ("n_cashflow_act", "经营活动现金流净额", "amount"),
    ("n_cashflow_inv_act", "投资活动现金流净额", "amount"),
    ("n_cash_flows_fnc_act", "筹资活动现金流净额", "amount"),
    ("free_cashflow", "自由现金流", "amount"),
    ("c_cash_equ_beg_period", "期初现金余额", "amount"),
    ("c_cash_equ_end_period", "期末现金余额", "amount"),
]

FORECAST_COLUMNS = [
    ("ann_date", "公告日", "date"),
    ("end_date", "报告期", "date"),
    ("type", "预告类型", "text"),
    ("_range", "业绩变动幅度", None),
    ("_profit_range", "净利润区间", None),
    ("last_parent_net", "上年同期归母净利", "amount"),
    ("summary", "摘要", "text80"),
]

EXPRESS_SPEC = [
    ("ann_date", "公告日", "date"),
    ("end_date", "报告期", "date"),
    ("revenue", "营业总收入", "amount"),
    ("n_income", "归母净利润", "amount"),
    ("yoy_net_profit", "净利同比", "pct"),
    ("diluted_eps", "每股收益(元)", "num3"),
    ("diluted_roe", "ROE", "pct"),
    ("bps", "每股净资产(元)", "num"),
]

DIVIDEND_COLUMNS = [
    ("end_date", "报告期", "date"),
    ("ann_date", "预案公告日", "date"),
    ("div_proc", "实施进度", "text"),
    ("cash_div_tax", "每股派息·税前(元)", "num3"),
    ("stk_bo_rate", "每股送股", "num"),
    ("stk_co_rate", "每股转增", "num"),
    ("record_date", "股权登记日", "date"),
    ("ex_date", "除权除息日", "date"),
]

HOLDER_COLUMNS = [
    ("holder_name", "股东名称", "text"),
    ("hold_amount", "持股数量", "shares"),
    ("hold_ratio", "持股比例", "pct"),
    ("hold_change", "较上期变动", "change"),
]

SHARE_FLOAT_COLUMNS = [
    ("float_date", "解禁日期", "date"),
    ("float_share", "解禁数量", "shares"),
    ("float_ratio", "占总股本", "pct"),
    ("holder_name", "持有人", "text"),
    ("share_type", "股份类型", "text"),
]

PLEDGE_COLUMNS = [
    ("end_date", "截止日期", "date"),
    ("pledge_count", "质押次数", "int"),
    ("unrest_pledge", "无限售股质押", "shares"),
    ("rest_pledge", "限售股质押", "shares"),
    ("pledge_ratio", "质押比例", "pct"),
]

REPURCHASE_COLUMNS = [
    ("ann_date", "公告日", "date"),
    ("proc", "实施进度", "text"),
    ("vol", "回购数量", "shares"),
    ("amount", "回购金额", "amount"),
    ("high_limit", "回购上限价", "num"),
    ("low_limit", "回购下限价", "num"),
]

MANAGER_COLUMNS = [
    ("name", "姓名", "text"),
    ("gender", "性别", "text"),
    ("title", "职务", "text"),
    ("edu", "学历", "text"),
    ("birthday", "出生日期", "text"),
    ("begin_date", "任职日期", "date"),
]

AUDIT_COLUMNS = [
    ("end_date", "报告期", "date"),
    ("audit_result", "审计结果", "text"),
    ("audit_fees", "审计费用", "amount"),
    ("audit_agency", "会计师事务所", "text"),
    ("audit_sign", "签字会计师", "text"),
]

REPORT_COLUMNS = [
    ("report_date", "日期", "date"),
    ("report_title", "研报标题", "text120"),
    ("org_name", "机构", "text"),
    ("author_name", "作者", "text"),
    ("op_rt", "评级", "text"),
    ("quarter", "预测季度", "text"),
    ("tp", "目标价", "num"),
]

# easy_tdx finance_info 标签映射（(raw, label, 格式)）
EASY_TDX_FINANCE_SPEC = [
    ("zong_guben", "总股本", "shares"),
    ("liutong_guben", "流通股本", "shares"),
    ("guojia_gu", "国家股", "shares"),
    ("faqiren_faren_gu", "发起法人股", "shares"),
    ("faren_gu", "法人股", "shares"),
    ("b_gu", "B股", "shares"),
    ("h_gu", "H股", "shares"),
    ("zhigong_gu", "职工股", "shares"),
    ("ipo_date", "上市日期", "date"),
    ("updated_date", "数据更新日期", "date"),
    ("gudong_renshu", "股东人数", "int"),
    ("zong_zichan", "总资产", "amount"),
    ("liudong_zichan", "流动资产", "amount"),
    ("guding_zichan", "固定资产", "amount"),
    ("wuxing_zichan", "无形资产", "amount"),
    ("cunhuo", "存货", "amount"),
    ("yingshou_zhangkuan", "应收账款", "amount"),
    ("liudong_fuzhai", "流动负债", "amount"),
    ("changqi_fuzhai", "长期负债", "amount"),
    ("ziben_gongjijin", "资本公积金", "amount"),
    ("jing_zichan", "净资产", "amount"),
    ("weifen_lirun", "未分配利润", "amount"),
    ("zhuying_shouru", "主营收入", "amount"),
    ("zhuying_lirun", "主营利润", "amount"),
    ("yingye_lirun", "营业利润", "amount"),
    ("touzi_shouyu", "投资收益", "amount"),
    ("lirun_zonghe", "利润总额", "amount"),
    ("jing_lirun", "净利润", "amount"),
    ("jingying_xianjinliu", "经营现金流", "amount"),
    ("zong_xianjinliu", "总现金流", "amount"),
    ("meigujing_zichan", "每股净资产(元)", "num"),
]


# ---------------------------------------------------------------------------
# 构造辅助
# ---------------------------------------------------------------------------


def _fields_section(key: str, title: str, pairs: list[tuple[str, str, str]]) -> FundamentalSection:
    fields = [
        FundamentalField(label=label, value=value, raw_key=raw)
        for raw, label, value in pairs
        if value not in (None, "", "--")
    ]
    return FundamentalSection(
        key=key,
        title=title,
        available=bool(fields),
        status="ok" if fields else "empty",
        field_count=len(fields),
        fields=fields,
    )


def _table_section(
    key: str,
    title: str,
    table_title: str,
    columns: list[str],
    rows: list[dict[str, Any]],
) -> FundamentalSection:
    table = FundamentalTable(title=table_title, columns=columns, rows=rows, row_count=len(rows))
    return FundamentalSection(
        key=key,
        title=title,
        available=bool(rows),
        status="ok" if rows else "empty",
        row_count=len(rows),
        tables=[table] if rows else [],
    )


def _records(df: Any) -> list[dict[str, Any]]:
    if df is None:
        return []
    try:
        if getattr(df, "empty", True):
            return []
        return df.to_dict("records")
    except Exception:
        return []


def _first(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return rows[0] if rows else {}


def _period_label(end_date: Any) -> str:
    text = str(end_date or "").strip()
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    return text or "--"


def _recent_period_rows(rows: list[dict[str, Any]], max_periods: int) -> list[dict[str, Any]]:
    """按报告期去重并倒序取最近 N 期（合并报表优先）。"""
    seen: set[str] = set()
    picked: list[dict[str, Any]] = []
    ordered = sorted(rows, key=lambda r: str(r.get("end_date") or ""), reverse=True)
    for row in ordered:
        end = str(row.get("end_date") or "")
        if not end or end in seen:
            continue
        comp_type = str(row.get("comp_type") or "")
        if comp_type and comp_type != "1":  # 1=合并报表
            continue
        seen.add(end)
        picked.append(row)
        if len(picked) >= max_periods:
            break
    if not picked:  # 数据没有 comp_type 时兜底
        for row in ordered:
            end = str(row.get("end_date") or "")
            if not end or end in seen:
                continue
            seen.add(end)
            picked.append(row)
            if len(picked) >= max_periods:
                break
    return picked


def _transposed_section(
    key: str,
    title: str,
    rows: list[dict[str, Any]],
    spec: list[tuple[str, str, str]],
    max_periods: int,
) -> FundamentalSection:
    period_rows = _recent_period_rows(rows, max_periods)
    if not period_rows:
        return FundamentalSection(key=key, title=title, available=False, status="empty")
    labels = [_period_label(r.get("end_date")) for r in period_rows]
    table_rows: list[dict[str, Any]] = []
    for raw, label, kind in spec:
        values = [_fmt(r.get(raw), kind) for r in period_rows]
        if all(v == "--" for v in values):
            continue
        row: dict[str, Any] = {"指标": label}
        for period, value in zip(labels, values):
            row[period] = value
        table_rows.append(row)
    return _table_section(key, title, title, ["指标", *labels], table_rows)


def _simple_table_section(
    key: str,
    title: str,
    rows: list[dict[str, Any]],
    column_spec: list[tuple[str, str, Any]],
    limit: int,
    row_mapper: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> FundamentalSection:
    columns = [label for _, label, _ in column_spec]
    table_rows: list[dict[str, Any]] = []
    for raw_row in rows[:limit]:
        if row_mapper is not None:
            table_rows.append(row_mapper(raw_row))
            continue
        mapped: dict[str, Any] = {}
        for raw, label, kind in column_spec:
            if kind == "text80":
                mapped[label] = _fmt_text(raw_row.get(raw), 80)
            elif kind == "text120":
                mapped[label] = _fmt_text(raw_row.get(raw), 120)
            else:
                mapped[label] = _fmt(raw_row.get(raw), kind or "text")
        table_rows.append(mapped)
    return _table_section(key, title, title, columns, table_rows)


def _category(key: str, title: str, sections: list[FundamentalSection], source: str = "tushare_pro") -> F10Category:
    available = any(section.available for section in sections)
    return F10Category(
        key=key,
        title=title,
        available=available,
        sections=[section for section in sections if section.available],
        source=source,
    )


def _category_error(key: str, title: str, exc: Exception) -> F10Category:
    logger.warning("tushare F10 category failed: %s error=%r", key, exc)
    return F10Category(key=key, title=title, available=False, error=str(exc)[:160])


# ---------------------------------------------------------------------------
# 主数据源
# ---------------------------------------------------------------------------


class TushareF10DataSource:
    """Tushare Pro 聚合 F10：公司概况/财务三表/股东/分红/资本运作/治理/研报。

    读取分三层：进程内存（TTL=fundamentals_cache_seconds）→ 持久缓存
    （F10CacheStore，新鲜度=f10_cache_seconds）→ 实时拉取。F10 属低频变化
    数据，持久缓存让重启/重部署后详情页依然秒开；盘前定时任务通过
    ``refresh_stale`` 对候选池做增量刷新。
    """

    SOURCE = "tushare_pro"

    def __init__(self, settings: Any, easy_tdx_f10: Any | None = None, store: Any | None = None) -> None:
        self.settings = settings
        self._easy_tdx = easy_tdx_f10
        self._store = store
        self._pro: Any | None = None
        self._cache: dict[str, tuple[float, F10Payload]] = {}

    # -- 基础 -------------------------------------------------------------

    def _token(self) -> str:
        env_token = os.getenv("WATCH_TUSHARE_TOKEN", "").strip()
        if env_token:
            return env_token
        secrets = getattr(self.settings, "secret_config", {}) or {}
        return str(secrets.get("tushare_token") or "").strip()

    def _client(self) -> Any:
        if self._pro is not None:
            return self._pro
        token = self._token()
        if not token:
            raise RuntimeError("未配置 tushare_token（ts2db_config.yaml 或 WATCH_TUSHARE_TOKEN）")
        import tushare as ts

        self._pro = ts.pro_api(token)
        return self._pro

    @staticmethod
    def _ts_code(code: str) -> str:
        normalized = str(code or "").strip().zfill(6)
        if normalized.startswith(("6", "9")):
            return f"{normalized}.SH"
        if normalized.startswith(("4", "8")):
            return f"{normalized}.BJ"
        return f"{normalized}.SZ"

    def _query(self, api_name: str, **kwargs: Any) -> list[dict[str, Any]]:
        api = getattr(self._client(), api_name)
        return _records(api(**kwargs))

    # -- 各分类 -------------------------------------------------------------

    def _cat_company_profile(self, ts_code: str) -> F10Category:
        basic = _first(self._query("stock_basic", ts_code=ts_code))
        company = _first(self._query("stock_company", ts_code=ts_code))
        status_map = {"L": "上市", "D": "退市", "P": "暂停上市"}
        hs_map = {"N": "否", "H": "沪港通", "S": "深港通", "SH": "沪深港通"}
        basic_pairs = [
            (raw, label, _fmt(basic.get(raw), kind))
            for raw, label, kind in COMPANY_BASIC_SPEC
        ]
        basic_pairs = [
            (raw, label, status_map.get(value, value) if raw == "list_status" else hs_map.get(value, value) if raw == "is_hs" else value)
            for raw, label, value in basic_pairs
        ]
        org_pairs = [(raw, label, _fmt(company.get(raw), kind)) for raw, label, kind in COMPANY_ORG_SPEC]
        text_sections = [
            FundamentalSection(
                key=f"company_text_{raw}",
                title=label,
                available=not _is_blank(company.get(raw)),
                status="ok" if not _is_blank(company.get(raw)) else "empty",
                field_count=1,
                fields=[FundamentalField(label=label, value=_fmt_text(company.get(raw), 1200), raw_key=raw)]
                if not _is_blank(company.get(raw))
                else [],
            )
            for raw, label in COMPANY_TEXT_SPEC
        ]
        sections = [
            _fields_section("company_basic", "基本信息", basic_pairs),
            _fields_section("company_org", "管理团队与联系", org_pairs),
            *text_sections,
        ]
        return _category("company_profile", "公司概况", sections)

    def _cat_indicators(self, ts_code: str) -> F10Category:
        rows = self._query("fina_indicator", ts_code=ts_code, limit=8)
        return _category(
            "fina_indicator",
            "主要财务指标",
            [_transposed_section("fina_indicator", "按报告期关键指标", rows, INDICATOR_SPEC, 6)],
        )

    def _cat_income(self, ts_code: str) -> F10Category:
        rows = self._query("income", ts_code=ts_code, limit=6)
        return _category(
            "income_statement",
            "利润表",
            [_transposed_section("income", "利润表·最近4期", rows, INCOME_SPEC, 4)],
        )

    def _cat_balance(self, ts_code: str) -> F10Category:
        rows = self._query("balancesheet", ts_code=ts_code, limit=6)
        return _category(
            "balance_sheet",
            "资产负债表",
            [_transposed_section("balancesheet", "资产负债表·最近4期", rows, BALANCE_SPEC, 4)],
        )

    def _cat_cashflow(self, ts_code: str) -> F10Category:
        rows = self._query("cashflow", ts_code=ts_code, limit=6)
        return _category(
            "cash_flow",
            "现金流量表",
            [_transposed_section("cashflow", "现金流量表·最近4期", rows, CASHFLOW_SPEC, 4)],
        )

    def _cat_performance(self, ts_code: str) -> F10Category:
        forecast_rows = self._query("forecast", ts_code=ts_code, limit=6)
        express_rows = self._query("express", ts_code=ts_code, limit=6)

        def forecast_mapper(row: dict[str, Any]) -> dict[str, Any]:
            lo, hi = row.get("p_change_min"), row.get("p_change_max")
            rng = "--" if _is_blank(lo) and _is_blank(hi) else f"{_fmt_num(lo, 0)}% ~ {_fmt_num(hi, 0)}%"
            plo, phi = row.get("net_profit_min"), row.get("net_profit_max")
            prng = "--" if _is_blank(plo) and _is_blank(phi) else f"{_fmt_amount(plo)} ~ {_fmt_amount(phi)}"
            return {
                "公告日": _fmt_date(row.get("ann_date")),
                "报告期": _fmt_date(row.get("end_date")),
                "预告类型": _fmt_text(row.get("type"), 20),
                "业绩变动幅度": rng,
                "净利润区间": prng,
                "上年同期归母净利": _fmt_amount(row.get("last_parent_net")),
                "摘要": _fmt_text(row.get("summary"), 80),
            }

        forecast_section = _simple_table_section(
            "forecast", "业绩预告", forecast_rows, FORECAST_COLUMNS, 6, row_mapper=forecast_mapper
        )
        express_section = _simple_table_section("express", "业绩快报", express_rows, EXPRESS_SPEC, 6)
        return _category("performance", "业绩预告与快报", [forecast_section, express_section])

    def _cat_main_business(self, ts_code: str) -> F10Category:
        rows = self._query("fina_mainbz", ts_code=ts_code, limit=60)
        if rows:
            latest = max(str(r.get("end_date") or "") for r in rows)
            rows = [r for r in rows if str(r.get("end_date") or "") == latest]
            rows.sort(key=lambda r: float(r.get("bz_sales") or 0), reverse=True)
        table_rows: list[dict[str, Any]] = []
        for row in rows[:14]:
            sales, cost = row.get("bz_sales"), row.get("bz_cost")
            margin = "--"
            try:
                if not _is_blank(sales) and float(sales):
                    margin = _fmt_pct((float(sales) - float(cost or 0)) / float(sales) * 100)
            except (TypeError, ValueError):
                pass
            table_rows.append(
                {
                    "报告期": _period_label(row.get("end_date")),
                    "构成项目": _fmt_text(row.get("bz_item"), 40),
                    "营业收入": _fmt_amount(sales),
                    "营业成本": _fmt_amount(cost),
                    "毛利": _fmt_amount(row.get("bz_profit")),
                    "毛利率": margin,
                }
            )
        section = _table_section(
            "main_business",
            "主营构成·最新报告期",
            "主营构成·最新报告期",
            ["报告期", "构成项目", "营业收入", "营业成本", "毛利", "毛利率"],
            table_rows,
        )
        return _category("main_business", "主营构成", [section])

    def _cat_shareholders(self, ts_code: str) -> F10Category:
        holder_num_rows = self._query("stk_holdernumber", ts_code=ts_code, limit=2)
        top10 = self._query("top10_holders", ts_code=ts_code, limit=10)
        top10_float = self._query("top10_floatholders", ts_code=ts_code, limit=10)

        num_pairs: list[tuple[str, str, str]] = []
        if holder_num_rows:
            latest = holder_num_rows[0]
            num_pairs.append(("end_date", "股东户数统计期", _fmt_date(latest.get("end_date"))))
            num_pairs.append(("holder_num", "股东户数", _fmt_int(latest.get("holder_num"))))
            if len(holder_num_rows) > 1:
                prev = holder_num_rows[1].get("holder_num")
                cur = latest.get("holder_num")
                try:
                    change = (float(cur) - float(prev)) / float(prev) * 100
                    num_pairs.append(("change", "较上期变化", f"{change:+.2f}%"))
                except (TypeError, ValueError, ZeroDivisionError):
                    pass

        def latest_period(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
            if not rows:
                return []
            latest = max(str(r.get("end_date") or "") for r in rows)
            return [r for r in rows if str(r.get("end_date") or "") == latest]

        period_note = ""
        if top10:
            period_note = f"（{_period_label(latest_period(top10)[0].get('end_date'))}）"
        sections = [
            _fields_section("holder_num", "股东户数", num_pairs),
            _simple_table_section("top10_holders", f"十大股东{period_note}", latest_period(top10), HOLDER_COLUMNS, 10),
            _simple_table_section("top10_floatholders", f"十大流通股东{period_note}", latest_period(top10_float), HOLDER_COLUMNS, 10),
        ]
        return _category("shareholders", "股东结构", sections)

    def _cat_dividend(self, ts_code: str) -> F10Category:
        rows = self._query("dividend", ts_code=ts_code, limit=8)
        return _category(
            "dividend",
            "分红送配",
            [_simple_table_section("dividend", "历年分红送配", rows, DIVIDEND_COLUMNS, 8)],
        )

    def _cat_capital_ops(self, ts_code: str) -> F10Category:
        float_rows = self._query("share_float", ts_code=ts_code, limit=8)
        pledge_rows = self._query("pledge_stat", ts_code=ts_code, limit=4)
        repurchase_rows = self._query("repurchase", ts_code=ts_code, limit=6)
        sections = [
            _simple_table_section("share_float", "限售解禁", float_rows, SHARE_FLOAT_COLUMNS, 8),
            _simple_table_section("pledge_stat", "股权质押", pledge_rows, PLEDGE_COLUMNS, 4),
            _simple_table_section("repurchase", "股份回购", repurchase_rows, REPURCHASE_COLUMNS, 6),
        ]
        return _category("capital_ops", "资本运作", sections)

    def _cat_governance(self, ts_code: str) -> F10Category:
        managers = self._query("stk_managers", ts_code=ts_code, limit=60)
        audit_rows = self._query("fina_audit", ts_code=ts_code, limit=3)
        if managers:
            latest = max(str(r.get("ann_date") or "") for r in managers)
            managers = [r for r in managers if str(r.get("ann_date") or "") == latest]
        sections = [
            _simple_table_section("managers", "高管成员", managers, MANAGER_COLUMNS, 16),
            _simple_table_section("audit", "审计意见", audit_rows, AUDIT_COLUMNS, 3),
        ]
        return _category("governance", "高管与审计", sections)

    def _cat_research(self, ts_code: str) -> F10Category:
        rows = self._query("report_rc", ts_code=ts_code, limit=10)
        return _category(
            "research_reports",
            "研报与盈利预测",
            [_simple_table_section("report_rc", "券商研报", rows, REPORT_COLUMNS, 10)],
        )

    def _cat_share_structure(self, code: str) -> F10Category:
        """easy_tdx finance_info 补充：股本结构与财务摘要。"""
        if self._easy_tdx is None:
            return F10Category(key="share_structure", title="股本结构", available=False, source="easy_tdx_f10_7615")
        payload = self._easy_tdx.fetch(code)
        finance = next((s for s in payload.sections if s.key == "finance_info"), None)
        row = {}
        if finance and finance.tables:
            row = finance.tables[0].rows[0] if finance.tables[0].rows else {}
        # easy_tdx section 表格只截 12 列，直接走底层 client 补全
        if len(row) < 30:
            try:
                row = self._easy_tdx_full_row(code) or row
            except Exception as exc:  # pragma: no cover
                logger.warning("easy_tdx finance_info full row failed: %r", exc)
        pairs = [(raw, label, _fmt(row.get(raw), kind)) for raw, label, kind in EASY_TDX_FINANCE_SPEC]
        section = _fields_section("share_structure", "股本结构与财务摘要（easy_tdx）", pairs)
        return _category("share_structure", "股本结构", [section], source="easy_tdx_f10_7615")

    def _easy_tdx_full_row(self, code: str) -> dict[str, Any] | None:
        from easy_tdx import TdxClient

        market = 1 if str(code).startswith("6") else 0
        with TdxClient(timeout=max(1.0, float(self.settings.easy_tdx_f10_timeout_seconds))) as client:
            df = client.get_finance_info(market, code)
        rows = _records(df)
        return rows[0] if rows else None

    # -- 日级资金流历史（详情页资金流面板用） --------------------------------------

    def fetch_moneyflow_daily(self, code: str, limit: int = 40) -> list[dict[str, Any]]:
        """tushare moneyflow 日级资金流历史，金额统一换算为元，按日期升序。

        主力 = 特大单 + 大单。easy_tdx 资金流只有最新一期单点，历史序列由这里补充；
        失败（限流/权限）返回空表，调用方降级为只展示单点数据。
        """
        normalized = str(code or "").strip().zfill(6)
        if len(normalized) != 6 or not normalized.isdigit():
            return []
        try:
            rows = self._query("moneyflow", ts_code=self._ts_code(normalized), limit=max(5, min(limit, 80)))
        except Exception as exc:
            logger.warning("tushare moneyflow failed: %s error=%r", normalized, exc)
            return []

        def yuan(row: dict[str, Any], key: str) -> float:
            try:
                return float(row.get(key) or 0) * 1e4  # tushare moneyflow 单位万元
            except (TypeError, ValueError):
                return 0.0

        out: list[dict[str, Any]] = []
        for row in rows:
            elg_net = yuan(row, "buy_elg_amount") - yuan(row, "sell_elg_amount")
            lg_net = yuan(row, "buy_lg_amount") - yuan(row, "sell_lg_amount")
            md_net = yuan(row, "buy_md_amount") - yuan(row, "sell_md_amount")
            sm_net = yuan(row, "buy_sm_amount") - yuan(row, "sell_sm_amount")
            out.append(
                {
                    "date": _fmt_date(row.get("trade_date")),
                    "main_net": elg_net + lg_net,
                    "elg_net": elg_net,
                    "lg_net": lg_net,
                    "md_net": md_net,
                    "sm_net": sm_net,
                }
            )
        out.sort(key=lambda r: str(r.get("date") or ""))
        return out

    # -- 聚合入口 -------------------------------------------------------------

    CATEGORY_BUILDERS: tuple[tuple[str, str, str], ...] = (
        ("company_profile", "公司概况", "_cat_company_profile"),
        ("fina_indicator", "主要财务指标", "_cat_indicators"),
        ("income_statement", "利润表", "_cat_income"),
        ("balance_sheet", "资产负债表", "_cat_balance"),
        ("cash_flow", "现金流量表", "_cat_cashflow"),
        ("performance", "业绩预告与快报", "_cat_performance"),
        ("main_business", "主营构成", "_cat_main_business"),
        ("shareholders", "股东结构", "_cat_shareholders"),
        ("dividend", "分红送配", "_cat_dividend"),
        ("capital_ops", "资本运作", "_cat_capital_ops"),
        ("governance", "高管与审计", "_cat_governance"),
        ("research_reports", "研报与盈利预测", "_cat_research"),
    )

    def _load_persistent(self, code: str, now_ts: float) -> F10Payload | None:
        """持久缓存命中（未超 f10_cache_seconds）时还原 payload 并回填内存。"""
        if self._store is None:
            return None
        max_age = max(0, int(getattr(self.settings, "f10_cache_seconds", 64800)))
        doc = self._store.load(code)
        if doc is None:
            return None
        if max_age and now_ts - float(doc["fetched_ts"]) > max_age:
            return None
        try:
            payload = F10Payload.model_validate(doc["payload"])
        except Exception as exc:
            logger.warning("f10 persistent cache invalid: %s error=%r", code, exc)
            return None
        self._cache[code] = (now_ts, payload)
        return payload

    def _load_persistent_stale(self, code: str) -> F10Payload | None:
        """不限新鲜度的兜底：实时拉取失败时宁可用过期缓存也不返回空。"""
        if self._store is None:
            return None
        doc = self._store.load(code)
        if doc is None:
            return None
        try:
            payload = F10Payload.model_validate(doc["payload"])
        except Exception:
            return None
        if payload.note:
            payload.note = f"{payload.note}；当前展示为缓存数据"
        else:
            payload.note = "实时拉取失败，当前展示为缓存数据"
        return payload

    def _save_persistent(self, code: str, payload: F10Payload, fetched_ts: float) -> None:
        if self._store is None:
            return
        self._store.save(code, payload.model_dump(mode="json"), fetched_ts)

    def refresh_stale(self, codes: list[str], max_age_seconds: int, limit: int = 80) -> dict[str, Any]:
        """盘前增量预热：只拉缓存缺失或超过 max_age_seconds 的股票。

        串行逐只刷新（每只内部仍并行拉 13 个分类），票间留 0.3s 间隔避免
        触发 tushare 限流。返回统计供日志/状态展示。
        """
        now_ts = time.time()
        unique: list[str] = []
        seen: set[str] = set()
        for raw in codes:
            text = str(raw or "").strip()
            if not text:
                continue
            code = text.zfill(6)
            if len(code) == 6 and code.isdigit() and code not in seen:
                seen.add(code)
                unique.append(code)
            if len(unique) >= limit:
                break
        stats: dict[str, Any] = {
            "candidates": len(unique),
            "refreshed": [],
            "skipped_fresh": 0,
            "failed": [],
        }
        for code in unique:
            age = self._store.age_seconds(code, now_ts) if self._store is not None else None
            if age is not None and age <= max_age_seconds:
                stats["skipped_fresh"] += 1
                continue
            try:
                payload = self.fetch(code, force=True)
            except Exception as exc:  # pragma: no cover - 网络/限流相关
                logger.warning("f10 preopen refresh failed: %s error=%r", code, exc)
                stats["failed"].append(code)
                continue
            if payload.available:
                stats["refreshed"].append(code)
            else:
                stats["failed"].append(code)
            time.sleep(0.3)
        return stats

    def fetch(self, code: str, force: bool = False) -> F10Payload:
        raw_code = str(code or "").strip()
        normalized = raw_code.zfill(6) if raw_code else ""
        if len(normalized) != 6 or not normalized.isdigit():
            return F10Payload(code=normalized, note="股票代码格式无效", expected_category_count=len(self.CATEGORY_BUILDERS) + 1)

        now_ts = time.time()
        ttl = max(0, int(getattr(self.settings, "fundamentals_cache_seconds", 3600)))
        cached = self._cache.get(normalized)
        if not force and ttl and cached and now_ts - cached[0] <= ttl:
            return cached[1].model_copy(deep=True)
        if not force:
            persistent = self._load_persistent(normalized, now_ts)
            if persistent is not None:
                return persistent.model_copy(deep=True)

        ts_code = self._ts_code(normalized)
        try:
            self._client()
        except Exception as exc:
            stale = self._load_persistent_stale(normalized)
            if stale is not None:
                stale.note = f"{stale.note}（{str(exc)[:120]}）"
                return stale
            return F10Payload(
                code=normalized,
                ts_code=ts_code,
                note=str(exc)[:200],
                expected_category_count=len(self.CATEGORY_BUILDERS) + 1,
            )

        results: dict[str, F10Category] = {}
        with ThreadPoolExecutor(max_workers=6) as executor:
            future_map = {
                executor.submit(getattr(self, method), ts_code): key for key, _, method in self.CATEGORY_BUILDERS
            }
            for future in as_completed(future_map):
                key = future_map[future]
                title = next(t for k, t, _ in self.CATEGORY_BUILDERS if k == key)
                try:
                    results[key] = future.result()
                except Exception as exc:  # pragma: no cover - 网络/限流相关
                    results[key] = _category_error(key, title, exc)
        try:
            results["share_structure"] = self._cat_share_structure(normalized)
        except Exception as exc:  # pragma: no cover
            results["share_structure"] = _category_error("share_structure", "股本结构", exc)

        ordered_keys = [key for key, _, _ in self.CATEGORY_BUILDERS] + ["share_structure"]
        categories = [results[key] for key in ordered_keys if key in results]
        available_count = sum(1 for c in categories if c.available)
        name = ""
        for category in categories:
            if category.key == "company_profile":
                for section in category.sections:
                    for field in section.fields:
                        if field.raw_key == "name":
                            name = str(field.value or "")
        payload = F10Payload(
            available=available_count > 0,
            code=normalized,
            ts_code=ts_code,
            name=name,
            fetched_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
            category_count=available_count,
            expected_category_count=len(ordered_keys),
            categories=categories,
        )
        self._cache[normalized] = (now_ts, payload)
        if payload.available:
            self._save_persistent(normalized, self._merge_with_cached(normalized, payload), now_ts)
        if len(self._cache) > 128:
            oldest = min(self._cache, key=lambda k: self._cache[k][0])
            self._cache.pop(oldest, None)
        return payload.model_copy(deep=True)

    def _merge_with_cached(self, code: str, payload: F10Payload) -> F10Payload:
        """写持久缓存前合并：新拉取的某个分类若因限流/异常失败，而旧缓存里该分类
        可用，则保留旧分类——避免盘前预热把 13/13 的完整缓存刷成 12/13 的残缺版。
        仅在持久层合并；内存与本次响应仍如实反映本次拉取结果。
        """
        if self._store is None:
            return payload
        doc = self._store.load(code)
        if not doc:
            return payload
        try:
            old = F10Payload.model_validate(doc["payload"])
        except Exception:
            return payload
        old_by_key = {c.key: c for c in old.categories}
        merged: list[F10Category] = []
        changed = False
        for cat in payload.categories:
            prev = old_by_key.get(cat.key)
            if not cat.available and cat.error and prev is not None and prev.available:
                merged.append(prev)
                changed = True
            else:
                merged.append(cat)
        if not changed:
            return payload
        payload.categories = merged
        payload.category_count = sum(1 for c in merged if c.available)
        return payload
