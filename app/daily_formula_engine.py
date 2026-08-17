from __future__ import annotations

"""AI量化主力狙击 日线公式引擎（公式/ 目录三套通达信公式的 point-in-time 复刻）。

数据源：easy_tdx 日K（前复权）。所有序列输入按时间升序。

口径说明（与通达信渲染对齐的工程取舍）：
- MA/HHV/LLV/SUM(X,N) 在窗口不足 N 根时用已有数据计算（min_periods=1），
  只影响序列最前面几十根；最近窗口（图表可视区）与通达信完全一致。
- DMA(X,A) 按通达信官方算法 Y=A*X+(1-A)*Y' 递推；官方文档要求 A<1，
  主图 SWS 的 MAX(1, 100*SUM(VOL,5)/(3*CAPITAL)) 恒 >=1，直接递推会发散，
  这里将 A 截断到 (0,1]（A=1 时 DMA=X，即 SWS≈EMA20，与通达信实际画出的
  白色点线一致）。
- CAPITAL（流通股本，单位股）由调用方传入快照值；缺失时 SWS/市场成本线/换手
  相关项降级（quality 标注）。
- INDEXC（对应大盘指数收盘）由调用方按日期对齐传入；缺失时大盘提示/个股提示
  中涉及 DSLX 的分支按中性处理（DSLX>=MA 视为 True），并降级标注。

输出分组：
- main: 主图（K线变色状态、SWL/SWS 红青带、LJX、市场成本线、强支撑、
  涨停/连板/炸板/短买/离场/超跌/抱团炒妖/高危 标记，及最新一根的
  量化评分H、个股提示、大盘提示、量变状态、明日价位）。
- sub_resonance: 副图1 AI主力双共振F（大单流向带 ZBCL7/8、主力动能 WWW、
  趋势波动线 TDXLFXJ、底部三层金条状态、★★反转拐点/控盘启动/双共振红灯）。
- sub_trend: 副图2 AI主力动向F（三路吸筹柱、趋势线、冲顶、牛股/前哨柱、
  机构/主力出货、红帽、红三角）。
- main_trend: 趋势线 tab 主图（主图公式.md：知行短期趋势线/知行多空线、
  FORCAST20 偏离度 K 线变色（红/绿/黄进/蓝出）、ATR 通道高位止盈/低位抄底）。
- sub_brick: 趋势线 tab 副图（副图公式.md：砖型图 + 短买/离场信号块）。
"""

from dataclasses import dataclass, field
import math
from typing import Any, Mapping, Sequence

from app.formula_engine import cross as _cross
from app.formula_engine import ema as _ema


EPS = 1e-12

# ---------------------------------------------------------------------------
# 通达信基础函数
# ---------------------------------------------------------------------------


def _num(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _ref(values: Sequence[float | None], n: int = 1) -> list[float | None]:
    shift = max(0, int(n))
    result: list[float | None] = [None] * len(values)
    for index in range(shift, len(values)):
        result[index] = values[index - shift]
    return result


def _ma(values: Sequence[float], period: int) -> list[float | None]:
    window = max(1, int(period))
    result: list[float | None] = []
    running = 0.0
    queue: list[float] = []
    for value in values:
        queue.append(_num(value))
        running += queue[-1]
        if len(queue) > window:
            running -= queue.pop(0)
        result.append(running / len(queue))
    return result


def _hhv(values: Sequence[float], period: int) -> list[float | None]:
    window = max(1, int(period))
    result: list[float | None] = []
    for index in range(len(values)):
        start = max(0, index - window + 1)
        result.append(max(_num(v) for v in values[start : index + 1]))
    return result


def _llv(values: Sequence[float], period: int) -> list[float | None]:
    window = max(1, int(period))
    result: list[float | None] = []
    for index in range(len(values)):
        start = max(0, index - window + 1)
        result.append(min(_num(v) for v in values[start : index + 1]))
    return result


def _sum(values: Sequence[float], period: int) -> list[float]:
    if period <= 0:  # SUM(X,0)：从首根起累计
        result: list[float] = []
        running = 0.0
        for value in values:
            running += _num(value)
            result.append(running)
        return result
    result = []
    running = 0.0
    queue: list[float] = []
    for value in values:
        queue.append(_num(value))
        running += queue[-1]
        if len(queue) > period:
            running -= queue.pop(0)
        result.append(running)
    return result


def _sma(values: Sequence[float], period: int, weight: int) -> list[float]:
    """TDX SMA(X,N,M)：Y=(X*M+Y'*(N-M))/N，首根以 X 起算。"""
    n = max(1, int(period))
    m = float(weight)
    result: list[float] = []
    previous: float | None = None
    for value in values:
        item = _num(value)
        previous = item if previous is None else (item * m + previous * (n - m)) / n
        result.append(previous)
    return result


def _dma(values: Sequence[float], alpha: Sequence[float]) -> list[float]:
    """TDX DMA(X,A) 动态移动平均：Y=A*X+(1-A)*Y'。

    官方要求 0<A<1；A 截断到 (0,1] 防止 A>=1 时递推发散（见模块 docstring）。
    """
    result: list[float] = []
    previous: float | None = None
    for index, value in enumerate(values):
        item = _num(value)
        a = _num(alpha[index] if index < len(alpha) else 0.0)
        a = min(max(a, 0.0), 1.0)
        previous = item if previous is None else a * item + (1.0 - a) * previous
        result.append(previous)
    return result


def _forcast(values: Sequence[float], period: int) -> list[float]:
    """TDX FORCAST(X,N)：N 周期线性回归对当前根的预测值。

    窗口内自变量取 0..m-1（旧→新），回归 y=a+b*t，返回 a+b*(m-1)。
    窗口不足 N 根时用已有数据（min_periods=1），单根窗口退化为该值本身。
    """
    window = max(1, int(period))
    result: list[float] = []
    for index in range(len(values)):
        start = max(0, index - window + 1)
        seg = [_num(v) for v in values[start : index + 1]]
        m = len(seg)
        if m < 2:
            result.append(seg[-1] if seg else 0.0)
            continue
        t_mean = (m - 1) / 2
        y_mean = sum(seg) / m
        num = sum((t - t_mean) * (y - y_mean) for t, y in enumerate(seg))
        den = sum((t - t_mean) ** 2 for t in range(m))
        slope = num / den if den > EPS else 0.0
        intercept = y_mean - slope * t_mean
        result.append(intercept + slope * (m - 1))
    return result


def _barslast(cond: Sequence[bool]) -> list[int]:
    """BARSLAST(X)：距上一次为真的周期数；当前为真则 0；从未为真则为当前下标。"""
    result: list[int] = []
    last_true = -1
    for index, flag in enumerate(cond):
        if flag:
            last_true = index
        result.append(index - last_true if last_true >= 0 else index + 10_000)
    return result


def _barslastcount(cond: Sequence[bool]) -> list[int]:
    """BARSLASTCOUNT(X)：连续为真的周期数（含当前根）。"""
    result: list[int] = []
    run = 0
    for flag in cond:
        run = run + 1 if flag else 0
        result.append(run)
    return result


def _and(*conds: Sequence[bool]) -> list[bool]:
    if not conds:
        return []
    length = len(conds[0])
    return [all(cond[i] for cond in conds) for i in range(length)]


def _or(*conds: Sequence[bool]) -> list[bool]:
    if not conds:
        return []
    length = len(conds[0])
    return [any(cond[i] for cond in conds) for i in range(length)]


def _ift(cond: Sequence[bool], a: Sequence[float] | float, b: Sequence[float] | float) -> list[float]:
    length = len(cond)
    a_seq = [float(a)] * length if isinstance(a, (int, float)) else [float(x) for x in a]
    b_seq = [float(b)] * length if isinstance(b, (int, float)) else [float(x) for x in b]
    return [a_seq[i] if cond[i] else b_seq[i] for i in range(length)]


def _cmp_gt(left: Sequence[float | None], right: Sequence[float | None] | float) -> list[bool]:
    if isinstance(right, (int, float)):
        return [bool(v is not None and v > right) for v in left]
    return [bool(a is not None and b is not None and a > b) for a, b in zip(left, right)]


def _cmp_ge(left: Sequence[float | None], right: Sequence[float | None] | float) -> list[bool]:
    if isinstance(right, (int, float)):
        return [bool(v is not None and v >= right) for v in left]
    return [bool(a is not None and b is not None and a >= b) for a, b in zip(left, right)]


def _round_series(values: Sequence[float | None], digits: int = 3) -> list[float | None]:
    return [None if v is None else round(float(v), digits) for v in values]


# ---------------------------------------------------------------------------
# 输入/输出
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DailyFormulaInput:
    dates: list[str]
    opens: list[float]
    highs: list[float]
    lows: list[float]
    closes: list[float]
    volumes: list[float]  # 股
    amounts: list[float] = field(default_factory=list)  # 元
    float_shares: float | None = None  # 流通股本（股）
    index_closes: list[float | None] | None = None  # 与 dates 对齐的大盘指数收盘
    winner_pct: float | None = None  # 最新收盘的获利比例（0..1），来自筹码分布引擎

    @classmethod
    def from_rows(
        cls,
        rows: Sequence[Mapping[str, Any]],
        *,
        float_shares: float | None = None,
        index_close_by_date: Mapping[str, float] | None = None,
        winner_pct: float | None = None,
    ) -> "DailyFormulaInput":
        dates: list[str] = []
        opens: list[float] = []
        highs: list[float] = []
        lows: list[float] = []
        closes: list[float] = []
        volumes: list[float] = []
        amounts: list[float] = []
        for row in rows:
            close = _num(row.get("close"))
            if close <= 0:
                continue
            dates.append(str(row.get("date") or ""))
            opens.append(_num(row.get("open"), close) or close)
            highs.append(_num(row.get("high"), close) or close)
            lows.append(_num(row.get("low"), close) or close)
            closes.append(close)
            volumes.append(max(_num(row.get("vol") or row.get("volume")), 0.0))
            amounts.append(max(_num(row.get("amount")), 0.0))
        index_closes: list[float | None] | None = None
        if index_close_by_date:
            index_closes = [
                (float(index_close_by_date[d]) if d in index_close_by_date else None)
                for d in dates
            ]
            # 前向填充：停牌日对齐上一交易日指数
            last_seen: float | None = None
            for index, value in enumerate(index_closes):
                if value is None:
                    index_closes[index] = last_seen
                else:
                    last_seen = value
        return cls(
            dates=dates,
            opens=opens,
            highs=highs,
            lows=lows,
            closes=closes,
            volumes=volumes,
            amounts=amounts,
            float_shares=float_shares,
            index_closes=index_closes,
            winner_pct=winner_pct,
        )


# ---------------------------------------------------------------------------
# 主图：AI量化主力狙击Z
# ---------------------------------------------------------------------------


def _hold_watch_states(closes: Sequence[float]) -> dict[str, list[bool]]:
    """红色持股/青色观望 13 态链（ABC1..ABC1A），逐根布尔。"""
    n = len(closes)
    c = list(closes)
    ref1 = [None] + c[:-1]
    ref2 = [None, None] + c[:-2]

    up = [i > 0 and c[i] > c[i - 1] for i in range(n)]
    down = [i > 0 and c[i] < c[i - 1] for i in range(n)]

    abc1 = [c[i] > (ref1[i] or 0) and c[i] > (ref2[i] or 0) if i >= 2 else False for i in range(n)]
    abcd = [c[i] < (ref1[i] if ref1[i] is not None else math.inf) and c[i] < (ref2[i] if ref2[i] is not None else math.inf) if i >= 2 else False for i in range(n)]

    # ABC2..ABCC：前一根满足上一态，且当日收盘介于 REF(C,1)/REF(C,2) 之间
    def mid_up(prev_state: list[bool]) -> list[bool]:
        out = [False] * n
        for i in range(1, n):
            if not prev_state[i - 1] or i < 1:
                continue
            r1 = c[i - 1]
            r2 = c[i - 2] if i >= 2 else None
            if r2 is None:
                continue
            out[i] = (c[i] <= r1 and c[i] >= r2) or (c[i] >= r1 and c[i] <= r2)
        return out

    def mid_down(prev_state: list[bool]) -> list[bool]:
        # ABCE..ABC18: REF(上一态,1) 且 C 位于 REF(C,1)/REF(C,2) 之间
        return mid_up(prev_state)

    up_states: list[list[bool]] = [abc1]
    for _ in range(11):  # ABC2..ABCC 共 11 个
        up_states.append(mid_up(up_states[-1]))
    down_states: list[list[bool]] = [abcd]
    for _ in range(11):  # ABCE..ABC18
        down_states.append(mid_down(down_states[-1]))

    hold = [_or(*[s for s in up_states])[i] for i in range(n)]
    watch = [_or(*[s for s in down_states])[i] for i in range(n)]
    # 短买：前一根处于任一观望态，本根满足 ABC1
    watch_ref = [False] + watch[:-1]
    short_buy = _and(watch_ref, abc1)
    # 白色离场：前一根处于任一持股态，本根满足 ABCD
    hold_ref = [False] + hold[:-1]
    white_exit = _and(hold_ref, abcd)
    return {"hold": hold, "watch": watch, "short_buy": short_buy, "white_exit": white_exit}


def _kdj(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], n: int = 9) -> tuple[list[float], list[float], list[float]]:
    hhv_h = _hhv(highs, n)
    llv_l = _llv(lows, n)
    rsv: list[float] = []
    for h, l, c in zip(hhv_h, llv_l, closes):
        span = (h or 0) - (l or 0)
        rsv.append(((c - (l or 0)) / span * 100.0) if span > EPS else 50.0)
    k = _sma(rsv, 3, 1)
    d = _sma(k, 3, 1)
    j = [3 * kk - 2 * dd for kk, dd in zip(k, d)]
    return k, d, j


def _macd(closes: Sequence[float]) -> tuple[list[float], list[float], list[float]]:
    dif = [a - b for a, b in zip(_ema(closes, 12), _ema(closes, 26))]
    dea = _ema(dif, 9)
    hist = [2 * (a - b) for a, b in zip(dif, dea)]
    return dif, dea, hist


def compute_main_chart(data: DailyFormulaInput) -> dict[str, Any]:
    n = len(data.closes)
    if n == 0:
        return {"available": False}
    c = data.closes
    o = data.opens
    h = data.highs
    l = data.lows
    v = data.volumes

    ema10 = _ema(c, 10)
    ema20 = _ema(c, 20)
    swl = [(a * 7 + b * 3) / 10 for a, b in zip(ema10, ema20)]
    if data.float_shares and data.float_shares > 0:
        sum_v5 = _sum(v, 5)
        sws_alpha = [max(1.0, 100.0 * (sv / (3.0 * data.float_shares))) for sv in sum_v5]
        turnover = [min(max(x / data.float_shares, 0.0), 1.0) for x in v]
    else:
        sws_alpha = [1.0] * n
        turnover = [0.0] * n
    sws = _dma(ema20, sws_alpha)
    ljx = ema20  # LJX: EMA(C,20)
    cost_line = _dma(c, turnover) if data.float_shares else [None] * n  # 市场成本线 DMA(C,换手)

    states = _hold_watch_states(c)
    hold = states["hold"]
    watch = states["watch"]

    # 涨停/连板/炸板
    ref_c = [None] + list(c[:-1])
    limit_up: list[bool] = []
    limit_up20: list[bool] = []
    broken: list[bool] = []
    for i in range(n):
        pc = ref_c[i]
        if pc is None or pc <= 0:
            limit_up.append(False)
            limit_up20.append(False)
            broken.append(False)
            continue
        ratio = c[i] / pc
        is_close_at_high = c[i] >= h[i] - max(EPS, h[i] * 1e-4)
        limit_up.append(ratio > 1.095 and is_close_at_high)
        limit_up20.append(ratio > 1.195 and is_close_at_high)
        broken.append(h[i] / pc > 1.095 and not is_close_at_high)
    zt_any = _or(limit_up, limit_up20)
    lianban = _barslastcount(zt_any)

    ma34 = _ma(c, 34)
    crash = [bool(m is not None and (c[i] - m) / m * 100 < -14) for i, m in enumerate(ma34)]

    # 抱团炒妖
    ma5 = _ma(c, 5)
    ma10 = _ma(c, 10)
    ma30 = _ma(c, 30)
    sum_cv = _sum([ci * vi * 100 for ci, vi in zip(c, v)], 26)
    sum_v26 = _sum([vi * 100 for vi in v], 26)
    avg26 = [int((a / b) * 100) / 100 if b > EPS else 0.0 for a, b in zip(sum_cv, sum_v26)]
    difq = [a - b for a, b in zip(_ema(c, 5), ema10)]
    deaq = _ema(difq, 9)
    baotuan: list[bool] = []
    for i in range(n):
        if i == 0 or ma5[i] is None or ma10[i] is None or ma30[i] is None:
            baotuan.append(False)
            continue
        cond = (
            o[i] <= ma5[i]
            and o[i] <= ma10[i]
            and o[i] <= ma30[i]
            and c[i] >= ma5[i]
            and c[i] >= avg26[i]
            and (deaq[i] - deaq[i - 1]) > 0
            and (difq[i] - difq[i - 1]) > 0
        )
        baotuan.append(cond)

    # 高危：乖离率>25 且收阴
    gaowei = [bool(ljx[i] and (c[i] - ljx[i]) / ljx[i] * 100 > 25 and c[i] < o[i]) for i in range(n)]

    # 强支撑：CONST(HHV(H,20)) * 0.809
    hhv20 = _hhv(h, 20)
    recent_high = hhv20[-1] or 0.0
    strong_support = recent_high * 0.809 if recent_high > 0 else None

    # 明日价位：E=(H+L+O+2C)/5
    e_last = (h[-1] + l[-1] + o[-1] + 2 * c[-1]) / 5
    tomorrow = {
        "resistance": round(2 * e_last - l[-1], 3),
        "support": round(2 * e_last - h[-1], 3),
        "breakthrough": round(e_last + (h[-1] - l[-1]), 3),
        "reverse": round(e_last - (h[-1] - l[-1]), 3),
    }

    # 量化评分H（最新一根，满分 80）
    k, d, j = _kdj(h, l, c)
    dif, dea, macd_hist = _macd(c)
    ma20 = _ma(c, 20)
    ma60 = _ma(c, 60)
    mav60 = _ma(v, 60)
    i = n - 1
    score = 0
    score += 20 if (ma5[i] is not None and ma10[i] is not None and ma5[i] > ma10[i]) else 0
    score += 10 if (ma20[i] is not None and ma60[i] is not None and ma20[i] > ma60[i]) else 0
    score += 10 if j[i] > k[i] else 0
    score += 10 if dif[i] > dea[i] else 0
    score += 10 if macd_hist[i] > 0 else 0
    score += 10 if (mav60[i] is not None and v[i] > mav60[i]) else 0
    score += 10 if (data.winner_pct is not None and data.winner_pct > 0.5) else 0
    score += 10 if (i >= 1 and ref_c[i] and c[i] / ref_c[i] > 1.03) else 0

    tips = _compute_tips(data, v, ma5, ma10, ma20)

    # 趋势公式.md：知行短期趋势线 EMA(EMA(C,10),10)（银色）、
    # 知行多空线 (MA14+MA28+MA57+MA114)/4。副图"趋势线"与做T面板的趋势值共用。
    zx_trend = _ema(_ema(c, 10), 10)
    ma14 = _ma(c, 14)
    ma28 = _ma(c, 28)
    ma57 = _ma(c, 57)
    ma114 = _ma(c, 114)
    zx_duokong = [
        (a + b + d + e) / 4.0 if None not in (a, b, d, e) else None
        for a, b, d, e in zip(ma14, ma28, ma57, ma114)
    ]
    trend_latest = {
        "ma5": round(ma5[-1], 3) if ma5[-1] is not None else None,
        "ma10": round(ma10[-1], 3) if ma10[-1] is not None else None,
        "ma20": round(ma20[-1], 3) if ma20[-1] is not None else None,
        "zx_trend": round(zx_trend[-1], 3) if zx_trend[-1] is not None else None,
        "zx_duokong": round(zx_duokong[-1], 3) if zx_duokong[-1] is not None else None,
    }

    candle_state = ["hold" if hold[idx] else "watch" if watch[idx] else "normal" for idx in range(n)]

    def idx_list(flags: Sequence[bool]) -> list[int]:
        return [idx for idx, flag in enumerate(flags) if flag]

    return {
        "available": True,
        "swl": _round_series(swl),
        "sws": _round_series(sws),
        "ljx": _round_series(ljx),
        "cost_line": _round_series(cost_line),
        "candle_state": candle_state,
        "markers": {
            "short_buy": idx_list(states["short_buy"]),
            "white_exit": idx_list(states["white_exit"]),
            "crash": idx_list(crash),
            "limit_up": idx_list(limit_up),
            "limit_up20": idx_list(limit_up20),
            "lianban": [{"index": idx, "count": lianban[idx]} for idx in range(n) if lianban[idx] >= 2],
            "broken": idx_list(broken),
            "baotuan": idx_list(baotuan),
            "gaowei": idx_list(gaowei),
        },
        "strong_support": round(strong_support, 3) if strong_support else None,
        "tomorrow": tomorrow,
        "score_h": score,
        "score_h_max": 80,
        "tips": tips,
        "ma5": _round_series(ma5),
        "ma10": _round_series(ma10),
        "ma20": _round_series(ma20),
        "zx_trend": _round_series(zx_trend),
        "zx_duokong": _round_series(zx_duokong),
        "trend_latest": trend_latest,
        "quality": {
            "float_shares": bool(data.float_shares),
            "index_close": bool(data.index_closes and any(x is not None for x in data.index_closes)),
            "winner": data.winner_pct is not None,
        },
    }


def _compute_tips(
    data: DailyFormulaInput,
    v: Sequence[float],
    ma5: Sequence[float | None],
    ma10: Sequence[float | None],
    ma20: Sequence[float | None],
) -> dict[str, Any]:
    """个股提示(16) / 大盘提示(8) / 量变状态(4)：互斥，取最新一根。"""
    n = len(data.closes)
    c = data.closes
    h = data.highs
    l = data.lows
    empty = {"stock": None, "market": None, "volume": None}
    if n < 3:
        return empty

    yun4 = ma20
    yunvx: list[float] = []
    for i in range(n):
        score = 0.0
        score += 10 if (ma5[i] is not None and c[i] > ma5[i]) else -10
        score += 10 if (ma5[i] is not None and ma10[i] is not None and ma5[i] > ma10[i]) else -10
        score += 10 if (ma10[i] is not None and c[i] > ma10[i]) else -10
        score += 10 if (ma5[i] is not None and ma20[i] is not None and ma5[i] > ma20[i]) else -10
        score += 10 if (ma20[i] is not None and c[i] > ma20[i]) else -10
        rising = i >= 1 and yun4[i] is not None and yun4[i - 1] is not None and yun4[i] > yun4[i - 1]
        score += 10 if rising else -10
        yunvx.append(score)

    yyv1 = _ma(v, 5)
    yyv2 = _ma(v, 10)

    if data.index_closes and any(x is not None for x in data.index_closes):
        dslx: list[float | None] = [
            (c[i] / idx) if idx and idx > EPS else None
            for i, idx in enumerate(data.index_closes)
        ]
        dslx_valid = [x if x is not None else 0.0 for x in dslx]
        dslx1 = _ma(dslx_valid, 5)
        dslx2 = _ma(dslx_valid, 10)
    else:
        dslx = [None] * n
        dslx1 = [None] * n
        dslx2 = [None] * n

    i = n - 1
    cross_up = yunvx[i - 1] <= 1 < yunvx[i]
    cross_down = yunvx[i - 1] >= 1 > yunvx[i]
    sustained_up = yunvx[i] >= 1 and yunvx[i - 1] > 0
    sustained_down = yunvx[i] < 1 and yunvx[i - 1] < 0
    yyv_ok = bool(yyv1[i] is not None and yyv2[i] is not None and yyv1[i] >= yyv2[i])
    # DSLX 缺失时按中性（>=MA 成立）处理
    if dslx[i] is None or dslx1[i] is None:
        dslx_ok = True
    else:
        dslx_ok = dslx[i] >= dslx1[i]

    stock_cases = [
        (cross_up and yyv_ok and dslx_ok, "量能理想，明显走强，中线参与，仓位2/3左右", "up"),
        (cross_up and not yyv_ok and dslx_ok, "走势趋强量能不足，短线进场，仓位1/2左右", "up"),
        (cross_up and yyv_ok and not dslx_ok, "量价良好，未明显走强，短线进场，仓位1/2左右", "up"),
        (cross_up and not yyv_ok and not dslx_ok, "形态尚可，量能不足，未完全走强，短线参与，仓位1/3左右", "flat"),
        (cross_down and yyv_ok and dslx_ok, "形态变坏，走势尚可，重仓减大半，清仓者观望", "down"),
        (cross_down and not yyv_ok and dslx_ok, "随大盘一起下跌，明显缩量走势，考虑大盘风险，减持或清仓", "down"),
        (cross_down and yyv_ok and not dslx_ok, "该股放量下跌，走势明显偏弱，离场观望", "down"),
        (cross_down and not yyv_ok and not dslx_ok, "该股缩量下跌，走势偏弱，离场观望", "down"),
        (sustained_up and yyv_ok and dslx_ok, "形态良好，强势明显，量能充足，可以继续持有", "up"),
        (sustained_up and not yyv_ok and dslx_ok, "上行趋势不变，强势依然，但量能开始转弱，注意风险", "flat"),
        (sustained_up and yyv_ok and not dslx_ok, "仍具备上涨潜力，但股性偏弱，可考虑减仓", "flat"),
        (sustained_up and not yyv_ok and not dslx_ok, "走势趋弱，量能不足，减持大部分筹码", "down"),
        (sustained_down and yyv_ok and dslx_ok, "形态不佳，注意成交量变化，目前不适合参与", "down"),
        (sustained_down and not yyv_ok and dslx_ok, "趋势不明，股价偏弱，注意量能变化，目前不宜参与", "down"),
        (sustained_down and yyv_ok and not dslx_ok, "有走强迹象，但量能不足，趋势不明，不适合操作", "flat"),
        (sustained_down and not yyv_ok and not dslx_ok, "走势太弱，没有参与价值", "down"),
    ]
    stock_tip = next(({"text": text, "tone": tone} for cond, text, tone in stock_cases if cond), None)

    market_cases = [
        (cross_up and yyv_ok, "总势量能理想，2/3仓位；前一轮下跌后需确认最佳买点，否则谨慎介入", "up"),
        (cross_up and not yyv_ok, "总势趋强，量能不足；若前一轮下跌，短线进场1/2，量能放大加仓", "flat"),
        (cross_down and yyv_ok, "总势形态走坏，重仓者可以减去大部分仓位", "down"),
        (cross_down and not yyv_ok, "总势缩量调整，注意风险，短线清仓观望", "down"),
        (sustained_up and yyv_ok, "总势走势良好，量能理想，可以继续持有强势股", "up"),
        (sustained_up and not yyv_ok, "总势形态良好，但量能稍显不足，随时准备减仓", "flat"),
        (sustained_down and yyv_ok, "总势趋势不明，注意成交量变化，目前不适宜进场", "flat"),
        (sustained_down and not yyv_ok, "总势趋势向下，量能萎缩，不适合进场", "down"),
    ]
    market_tip = next(({"text": text, "tone": tone} for cond, text, tone in market_cases if cond), None)

    mfi = [(h[idx] - l[idx]) * 1_000_000 / v[idx] if v[idx] > EPS else 0.0 for idx in range(n)]
    mfi_up = mfi[i] >= mfi[i - 1]
    vol_up = v[i] >= v[i - 1]
    if mfi_up and vol_up:
        volume_tip = {"text": "绿灯：实计交易量增加，MFI促进指数增加", "tone": "up"}
    elif not mfi_up and not vol_up:
        volume_tip = {"text": "衰退：实计交易量减少，MFI促进指数减少", "tone": "down"}
    elif mfi_up and not vol_up:
        volume_tip = {"text": "伪装：实计交易量减少，MFI促进指数增加", "tone": "flat"}
    else:
        volume_tip = {"text": "蛰伏：实计交易量增加，MFI促进指数减少", "tone": "flat"}

    return {
        "stock": stock_tip,
        "market": market_tip,
        "volume": volume_tip,
        "yunvx": round(yunvx[i], 1),
    }


# ---------------------------------------------------------------------------
# 副图1：AI主力双共振F
# ---------------------------------------------------------------------------


def compute_sub_resonance(data: DailyFormulaInput) -> dict[str, Any]:
    n = len(data.closes)
    if n == 0:
        return {"available": False}
    c = data.closes
    h = data.highs
    l = data.lows
    v = data.volumes
    amounts = data.amounts if len(data.amounts) == n else [0.0] * n

    # 大单流向带（累计口径 SUM(X,0)）
    zbcl1 = [vi / ci / 2 if ci > EPS else 0.0 for ci, vi in zip(c, v)]
    ref_c = [None] + list(c[:-1])
    up_day = [bool(ref_c[i] is not None and c[i] > ref_c[i]) for i in range(n)]
    down_day = [bool(ref_c[i] is not None and c[i] < ref_c[i]) for i in range(n)]
    zbcl2 = _sum([zbcl1[i] if (zbcl1[i] > 100 and up_day[i]) else 0.0 for i in range(n)], 0)
    zbcl3 = _sum([zbcl1[i] if (zbcl1[i] > 100 and down_day[i]) else 0.0 for i in range(n)], 0)
    zbcl4 = _sum([zbcl1[i] if (zbcl1[i] < 100 and up_day[i]) else 0.0 for i in range(n)], 0)
    zbcl5 = _sum([zbcl1[i] if (zbcl1[i] < 100 and down_day[i]) else 0.0 for i in range(n)], 0)
    zbcl6 = [zbcl2[i] + zbcl3[i] + zbcl4[i] + zbcl5[i] for i in range(n)]
    z7 = [(zbcl2[i] / zbcl6[i]) * 100 - 100 if zbcl6[i] > EPS else -100.0 for i in range(n)]
    z8 = [(zbcl3[i] / zbcl6[i]) * 100 - 100 if zbcl6[i] > EPS else -100.0 for i in range(n)]

    # 主力控盘动能 WWW（红绿山丘）
    zblj1 = [(c[i] * 2 + h[i] + l[i]) / 4 * 10 for i in range(n)]
    zblj2 = [a - b for a, b in zip(_ema(zblj1, 13), _ema(zblj1, 34))]
    zblj3 = _ema(zblj2, 5)
    zblj4 = [2 * (a - b) * 5.5 for a, b in zip(zblj2, zblj3)]
    www = [x * 3 if x >= 0 else 0.0 for x in zblj4]

    # 趋势波动线 TDXLFXJ
    llv34 = _llv(l, 34)
    hhv21 = _hhv(h, 21)
    d_series = [
        ((c[i] - llv34[i]) / (hhv21[i] - llv34[i]) * 100) if (hhv21[i] - llv34[i]) > EPS else 50.0
        for i in range(n)
    ]
    tdxlfxj = [(a - 50) * 2 for a in _ema(d_series, 3)]

    # 底部三层状态条
    expma10 = _ema(c, 10)
    expma60 = _ema(c, 60)
    strip_weak = [expma10[i] < expma60[i] for i in range(n)]  # 绿 0-10
    strip_mid = [expma10[i] > expma60[i] for i in range(n)]  # 黄 15-25
    # 大周期机构成本线：累计 VWAP（amount 元 / vol 股）
    cum_amount = _sum(amounts, 0)
    cum_vol = _sum(v, 0)
    cum_vwap = [cum_amount[i] / cum_vol[i] if cum_vol[i] > EPS else c[i] for i in range(n)]
    ratio = [c[i] / cum_vwap[i] if cum_vwap[i] > EPS else 1.0 for i in range(n)]
    expma120_ratio = _ema(ratio, 120)
    strip_top = [x > 1.003 for x in expma120_ratio]  # 黄 30-40

    # 控盘启动
    n35, m35, n1 = 35, 35, 3
    hhv35 = _hhv(h, n35)
    llv35 = _llv(l, n35)
    abc1 = [
        ((hhv35[i] - c[i]) / (hhv35[i] - llv35[i]) * 100 - m35) if (hhv35[i] - llv35[i]) > EPS else 0.0
        for i in range(n)
    ]
    abc2 = [x + 100 for x in _sma(abc1, n35, 1)]
    abc3 = [
        ((c[i] - llv35[i]) / (hhv35[i] - llv35[i]) * 100) if (hhv35[i] - llv35[i]) > EPS else 0.0
        for i in range(n)
    ]
    abc4 = _sma(abc3, 3, 1)
    abc5 = [x + 100 for x in _sma(abc4, 3, 1)]
    abc6 = [a - b for a, b in zip(abc5, abc2)]
    kongqi = [(x - n1 + 2) * 2.5 if x > n1 else 0.0 for x in abc6]
    kq_run = _barslastcount([x > n1 for x in abc6])
    start = [run == 1 for run in kq_run]
    kq_cross = _cross(kongqi, 100.0)

    # 极致反转：J 上穿 0（CROSS(J, REVERSE(J)) ⟺ J 上穿 -J ⟺ J 由负转正）
    _, _, j = _kdj(h, l, c, 9)
    j_cross_zero = _cross(j, [0.0] * n)
    ma60 = _ma(c, 60)
    aa = [
        bool(i >= 10 and ma60[i - 10] is not None and c[i - 10] > ma60[i - 10])
        for i in range(n)
    ]
    reversal = _and(j_cross_zero, aa)

    red_light = [www[i] > 20 and tdxlfxj[i] > 0 for i in range(n)]

    def idx_list(flags: Sequence[bool]) -> list[int]:
        return [idx for idx, flag in enumerate(flags) if flag]

    return {
        "available": True,
        "z7": _round_series(z7, 2),
        "z8": _round_series(z8, 2),
        "www": _round_series(www, 2),
        "tdxlfxj": _round_series(tdxlfxj, 2),
        "kongqi": _round_series(kongqi, 2),
        "strip_weak": [1 if x else 0 for x in strip_weak],
        "strip_mid": [1 if x else 0 for x in strip_mid],
        "strip_top": [1 if x else 0 for x in strip_top],
        "markers": {
            "reversal": idx_list(reversal),  # ★★反转拐点（洋红柱 50-95）
            "start": idx_list(start),  # 启动（浅蓝柱 30-40）
            "kongqi_cross": idx_list(kq_cross),  # 控盘启动上穿 100
            "red_light": idx_list(red_light),  # 双共振红灯
        },
        "latest": {
            "red_light": bool(red_light[-1]),
            "www": round(www[-1], 2),
            "tdxlfxj": round(tdxlfxj[-1], 2),
        },
    }


# ---------------------------------------------------------------------------
# 副图2：AI主力动向F
# ---------------------------------------------------------------------------


def compute_sub_trend(data: DailyFormulaInput) -> dict[str, Any]:
    n = len(data.closes)
    if n == 0:
        return {"available": False}
    c = data.closes
    h = data.highs
    l = data.lows

    llv55 = _llv(l, 55)
    hhv55 = _hhv(h, 55)
    bnge1 = [
        ((c[i] - llv55[i]) / (hhv55[i] - llv55[i]) * 100) if (hhv55[i] - llv55[i]) > EPS else 50.0
        for i in range(n)
    ]
    sma_b1 = _sma(bnge1, 5, 1)
    bnge2 = _ema(
        [3 * a - 2 * b for a, b in zip(sma_b1, _sma(sma_b1, 3, 1))],
        3,
    )

    def accum(period: int, cap: float) -> list[float]:
        ref_l = [None] + list(l[:-1])
        diff = [abs(l[i] - ref_l[i]) if ref_l[i] is not None else 0.0 for i in range(n)]
        pos = [max(l[i] - ref_l[i], 0.0) if ref_l[i] is not None else 0.0 for i in range(n)]
        sma_abs = _sma(diff, 3, 1)
        sma_pos = _sma(pos, 3, 1)
        g3 = [sma_abs[i] / sma_pos[i] * 100 if sma_pos[i] > EPS else 0.0 for i in range(n)]
        g4 = _ema([x * 10 for x in g3], 3)
        g5 = _llv(l, period)
        g6 = _hhv(g4, period)
        inner = [
            ((g4[i] + g6[i] * 2) / 2) if l[i] <= g5[i] else 0.0
            for i in range(n)
        ]
        g7 = [x / 618 for x in _ema(inner, 3)]
        return [min(x, cap) for x in g7]

    bnge8 = accum(38, 100.0)  # 主力吸筹（红柱，封顶100）
    bnge14 = accum(13, 500.0)  # 发财吸筹（灰柱，封顶500）

    # KDJ 族（9,3,3）用于前哨/黄粉针
    bnge15_hhv = _hhv(h, 9)
    bnge15_llv = _llv(l, 9)
    bnge15 = [
        ((c[i] - bnge15_llv[i]) / (bnge15_hhv[i] - bnge15_llv[i]) * 100)
        if (bnge15_hhv[i] - bnge15_llv[i]) > EPS
        else 50.0
        for i in range(n)
    ]
    bnge16 = _sma(bnge15, 3, 1)
    bnge17 = _sma(bnge16, 3, 1)
    bnge18 = [3 * a - 2 * b for a, b in zip(bnge16, bnge17)]

    bnge27_llv = _llv(l, 9)
    bnge27_hhv = _hhv(h, 9)
    bnge27 = [
        ((c[i] - bnge27_llv[i]) / (bnge27_hhv[i] - bnge27_llv[i]) * 100)
        if (bnge27_hhv[i] - bnge27_llv[i]) > EPS
        else 50.0
        for i in range(n)
    ]
    bnge28 = _sma(bnge27, 3, 1)
    bnge29 = _sma(bnge28, 3, 1)

    cross_28_29 = _cross(bnge28, bnge29)
    cross_29_28 = _cross(bnge29, bnge28)
    cross_27_28 = _cross(bnge27, bnge28)
    cross_28_27 = _cross(bnge28, bnge27)

    barslast_29_28 = _barslast(cross_29_28)
    bnge30 = _and(cross_28_29, [x < 20 for x in bnge28])
    bnge31 = _and([b < 9 for b in barslast_29_28], bnge30)
    niu = _and([b < 9 for b in barslast_29_28], bnge31)  # 牛股白柱

    barslast_28_27 = _barslast(cross_28_27)
    bnge32 = _and(cross_27_28, [x < 30 for x in bnge28])
    shao = _and([b >= 3 for b in barslast_28_27], bnge32)  # 前哨洋红柱（高60）

    # 黄柱/粉柱（试盘针）
    j_prev1 = [None] + bnge18[:-1]
    j_prev2 = [None, None] + bnge18[:-2]
    j_turn_up = [
        bool(j_prev1[i] is not None and j_prev2[i] is not None and j_prev1[i] < j_prev2[i])
        for i in range(n)
    ]
    bnge34 = _and([x > 1 for x in bnge14], [bnge18[i] > (j_prev1[i] or -math.inf) for i in range(n)])
    bnge35 = _and(
        [0.1 < x < 1 for x in bnge14],
        [bnge18[i] > (j_prev1[i] or -math.inf) for i in range(n)],
    )
    yellow_pin = _and(bnge34, j_turn_up)
    pink_pin = _and(bnge35, j_turn_up)

    # 冲顶
    hhv13c = _hhv(c, 13)
    llv13c = _llv(c, 13)
    bnge26 = _sma(
        [
            ((hhv13c[i] - c[i]) / (hhv13c[i] - llv13c[i])) if (hhv13c[i] - llv13c[i]) > EPS else 0.0
            for i in range(n)
        ],
        5,
        1,
    )
    chongding = [2 / x - 2 if x > EPS else 0.0 for x in bnge26]

    # 机构/主力出货（30 周期位置）
    va1_llv = _llv(l, 30)
    va1_hhv = _hhv(h, 30)
    va1 = [
        ((c[i] - va1_llv[i]) / (va1_hhv[i] - va1_llv[i]) * 100) if (va1_hhv[i] - va1_llv[i]) > EPS else 50.0
        for i in range(n)
    ]
    va2 = _sma(va1, 2, 1)
    va3 = _sma(va2, 2, 1)
    jigou_chu = _and([x > 89.5 for x in va3], [x > 91 for x in va2])
    zhuli_chu = _and([x > 88.5 for x in va3], [x > 94.6 for x in va2])

    # 红帽：吸筹资金由强转弱的第一根
    bnge25 = [bnge14[i] < (bnge14[i - 1] if i >= 1 else 0.0) for i in range(n)]
    red_hat = _and(bnge25, [not bnge25[i - 1] if i >= 1 else False for i in range(n)])
    # 红三角：趋势线 BNGE2 上穿 10
    red_triangle = _cross(bnge2, 10.0)

    trend_accum = [x if x <= 14 else None for x in bnge2]  # 趋势吸筹（青色，仅低位显示）
    trend_line = [x - 10 for x in bnge2]

    def idx_list(flags: Sequence[bool]) -> list[int]:
        return [idx for idx, flag in enumerate(flags) if flag]

    return {
        "available": True,
        "trend_line": _round_series(trend_line, 2),
        "trend_accum": _round_series(trend_accum, 2),
        "main_accum": _round_series(bnge8, 2),
        "rich_accum": _round_series(bnge14, 2),
        "chongding": _round_series(chongding, 2),
        "markers": {
            "niu": idx_list(niu),
            "shao": idx_list(shao),
            "yellow_pin": idx_list(yellow_pin),
            "pink_pin": idx_list(pink_pin),
            "jigou_chu": idx_list(jigou_chu),
            "zhuli_chu": idx_list(zhuli_chu),
            "red_hat": idx_list(red_hat),
            "red_triangle": idx_list(red_triangle),
        },
        "latest": {
            "jigou_chu": bool(jigou_chu[-1]),
            "zhuli_chu": bool(zhuli_chu[-1]),
            "niu": bool(niu[-1]),
        },
    }


# ---------------------------------------------------------------------------
# 趋势线 tab 主图（主图公式.md）
# ---------------------------------------------------------------------------


def compute_main_trend_chart(data: DailyFormulaInput) -> dict[str, Any]:
    """知行趋势主图：短期趋势线/多空线 + 偏离度变色 K 线 + ATR 通道突破提示。

    - 知行短期趋势线 EMA(EMA(C,10),10)（白细线）
    - 知行多空线 (MA14+MA28+MA57+MA114)/4（黄粗线）
    - 偏差 = (C - FORCAST(C,20))/C*100：>0 红 K / <0 绿 K；上拐黄 K、下拐蓝 K
    - ATR 通道：P = MA(TR,30)/C*100*0.8；CROSS(P,偏差) 高位止盈（蓝）、
      CROSS(偏差,-P) 低位抄底（黄）
    """
    n = len(data.closes)
    if n == 0:
        return {"available": False}
    c = data.closes
    h = data.highs
    l = data.lows

    zx_trend = _ema(_ema(c, 10), 10)
    ma14 = _ma(c, 14)
    ma28 = _ma(c, 28)
    ma57 = _ma(c, 57)
    ma114 = _ma(c, 114)
    zx_duokong = [
        (a + b + d + e) / 4.0 if None not in (a, b, d, e) else None
        for a, b, d, e in zip(ma14, ma28, ma57, ma114)
    ]

    forecast = _forcast(c, 20)
    deviation = [
        ((c[i] - forecast[i]) / c[i] * 100.0) if c[i] > EPS else 0.0
        for i in range(n)
    ]

    candle_state: list[str] = []
    for i in range(n):
        dev = deviation[i]
        prev = deviation[i - 1] if i >= 1 else 0.0
        if dev > 0 and prev < 0:
            candle_state.append("turn_up")
        elif dev < 0 and prev > 0:
            candle_state.append("turn_down")
        elif dev > 0:
            candle_state.append("up")
        elif dev < 0:
            candle_state.append("down")
        else:
            candle_state.append("normal")

    ref_c = [None] + list(c[:-1])
    tr1 = [
        max(
            h[i] - l[i],
            abs((ref_c[i] if ref_c[i] is not None else c[i]) - h[i]),
            abs((ref_c[i] if ref_c[i] is not None else c[i]) - l[i]),
        )
        for i in range(n)
    ]
    atr14 = [
        (m / c[i] * 100.0) if m is not None and c[i] > EPS else 0.0
        for i, m in enumerate(_ma(tr1, 30))
    ]
    p_band = [x * 0.8 for x in atr14]
    neg_band = [-x for x in p_band]
    high_exit = _cross(p_band, deviation)  # CROSS(P, 偏离值) 高位止盈
    low_buy = _cross(deviation, neg_band)  # CROSS(偏离值, -P) 低位抄底

    return {
        "available": True,
        "zx_trend": _round_series(zx_trend),
        "zx_duokong": _round_series(zx_duokong),
        "deviation": _round_series(deviation, 2),
        "candle_state": candle_state,
        "markers": {
            "atr_high": [
                {"index": i, "price": round(h[i], 3)} for i, flag in enumerate(high_exit) if flag
            ],
            "atr_low": [
                {"index": i, "price": round(l[i], 3)} for i, flag in enumerate(low_buy) if flag
            ],
        },
        "latest": {
            "deviation": round(deviation[-1], 2),
            "zx_trend": round(zx_trend[-1], 3),
            "zx_duokong": round(zx_duokong[-1], 3) if zx_duokong[-1] is not None else None,
        },
    }


# ---------------------------------------------------------------------------
# 趋势线 tab 副图（副图公式.md）：砖型图 + 短买/离场
# ---------------------------------------------------------------------------


def compute_sub_brick(data: DailyFormulaInput) -> dict[str, Any]:
    """砖型图副图：砖块值 砖型图=IF(VAR6A>4,VAR6A-4,0)，红涨绿跌白平；
    短买/离场沿用与 AI主力狙击Z 同一结构的 13 态链（_hold_watch_states）。"""
    n = len(data.closes)
    if n == 0:
        return {"available": False}
    c = data.closes
    h = data.highs
    l = data.lows

    hhv4 = _hhv(h, 4)
    llv4 = _llv(l, 4)
    span = [hhv4[i] - llv4[i] for i in range(n)]
    var1a = [
        ((hhv4[i] - c[i]) / span[i] * 100.0 - 90.0) if span[i] > EPS else -40.0
        for i in range(n)
    ]
    var2a = [x + 100.0 for x in _sma(var1a, 4, 1)]
    var3a = [
        ((c[i] - llv4[i]) / span[i] * 100.0) if span[i] > EPS else 50.0
        for i in range(n)
    ]
    var4a = _sma(var3a, 6, 1)
    var5a = [x + 100.0 for x in _sma(var4a, 6, 1)]
    var6a = [a - b for a, b in zip(var5a, var2a)]
    brick = [x - 4.0 if x > 4.0 else 0.0 for x in var6a]

    states = _hold_watch_states(c)

    def idx_list(flags: Sequence[bool]) -> list[int]:
        return [idx for idx, flag in enumerate(flags) if flag]

    return {
        "available": True,
        "brick": _round_series(brick, 2),
        "markers": {
            "short_buy": idx_list(states["short_buy"]),
            "exit": idx_list(states["white_exit"]),
        },
        "latest": {
            "brick": round(brick[-1], 2),
            "short_buy": bool(states["short_buy"][-1]),
            "exit": bool(states["white_exit"][-1]),
        },
    }


# ---------------------------------------------------------------------------
# 汇总入口
# ---------------------------------------------------------------------------


def compute_daily_formulas(data: DailyFormulaInput) -> dict[str, Any]:
    return {
        "main": compute_main_chart(data),
        "sub_resonance": compute_sub_resonance(data),
        "sub_trend": compute_sub_trend(data),
        "main_trend": compute_main_trend_chart(data),
        "sub_brick": compute_sub_brick(data),
    }


__all__ = [
    "DailyFormulaInput",
    "compute_daily_formulas",
    "compute_main_chart",
    "compute_main_trend_chart",
    "compute_sub_brick",
    "compute_sub_resonance",
    "compute_sub_trend",
]
