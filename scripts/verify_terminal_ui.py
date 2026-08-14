"""Playwright acceptance checks for the current local trading terminal."""

from __future__ import annotations

import argparse
import json
import math
import time
from pathlib import Path
from typing import Any

from playwright.sync_api import Browser, Page, sync_playwright


DEFAULT_BASE_URL = "http://127.0.0.1:8788"
DEFAULT_SCREENSHOT_DIR = Path("data/runtime/ui-check")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--screenshot-dir", type=Path, default=DEFAULT_SCREENSHOT_DIR)
    return parser.parse_args()


def box_metrics(page: Page, selector: str) -> dict[str, Any]:
    locator = page.locator(selector)
    if not locator.count():
        return {"missing": True}
    return locator.first.evaluate(
        """element => {
            const rect = element.getBoundingClientRect();
            return {
                x: rect.x, y: rect.y, width: rect.width, height: rect.height,
                clientWidth: element.clientWidth, clientHeight: element.clientHeight,
                scrollWidth: element.scrollWidth, scrollHeight: element.scrollHeight,
                overflowX: Math.max(0, element.scrollWidth - element.clientWidth),
                overflowY: Math.max(0, element.scrollHeight - element.clientHeight),
            };
        }"""
    )


def canvas_metrics(page: Page, selector: str) -> dict[str, Any]:
    locator = page.locator(selector)
    if not locator.count():
        return {"count": 0, "widths": [], "heights": [], "data_lengths": []}
    return locator.first.evaluate(
        """element => {
            const canvases = [...element.querySelectorAll('canvas')];
            return {
                count: canvases.length,
                widths: canvases.map(canvas => canvas.width),
                heights: canvases.map(canvas => canvas.height),
                data_lengths: canvases.map(canvas => canvas.toDataURL('image/png').length),
            };
        }"""
    )


def mini_chart_metrics(page: Page) -> dict[str, Any]:
    return page.evaluate(
        """() => {
            const boxes = [...document.querySelectorAll('.mini-chart-box')];
            const usable = boxes.filter(box => {
                const price = box.querySelector('.mini-price');
                return price && String(price.getAttribute('points') || '').split(' ').length >= 8;
            });
            return {
                count: boxes.length,
                usable_count: usable.length,
                empty_count: document.querySelectorAll('.mini-chart-empty').length,
                sector_count: document.querySelectorAll('.sector-mini .mini-chart-box').length,
                board_count: document.querySelectorAll('#stockRows .col-mini .mini-chart-box').length,
                watch_count: document.querySelectorAll('.watch-mini .mini-chart-box').length,
            };
        }"""
    )


def document_metrics(page: Page) -> dict[str, Any]:
    return page.evaluate(
        """() => ({
            viewport_width: window.innerWidth,
            viewport_height: window.innerHeight,
            scroll_width: document.documentElement.scrollWidth,
            scroll_height: document.documentElement.scrollHeight,
            scroll_y: window.scrollY,
            overflow_x: Math.max(0, document.documentElement.scrollWidth - window.innerWidth),
        })"""
    )


def check_detail_tabs(page: Page) -> dict[str, Any]:
    expected = ["messages", "news", "fundamentals", "capital_flow", "chanlun"]
    initial_active = page.locator("[data-detail-tab][aria-selected='true']").first.get_attribute("data-detail-tab") or ""
    rows: list[dict[str, Any]] = []
    for tab in expected:
        button = page.locator(f'[data-detail-tab="{tab}"]')
        if not button.count():
            rows.append({"tab": tab, "missing": True, "active": False, "pane_visible": False})
            continue
        control = button.first.get_attribute("aria-controls") or ""
        button.first.click()
        page.wait_for_timeout(80)
        pane_visible = page.evaluate(
            """control => {
                const pane = document.getElementById(control);
                return Boolean(pane && !pane.hidden && getComputedStyle(pane).display !== 'none');
            }""",
            control,
        )
        rows.append(
            {
                "tab": tab,
                "missing": False,
                "active": button.first.get_attribute("aria-selected") == "true",
                "pane": control,
                "pane_visible": pane_visible,
            }
        )
    page.locator('[data-detail-tab="messages"]').click()
    page.wait_for_timeout(80)
    return {
        "initial_active": initial_active,
        "rows": rows,
        "all_usable": all((not item.get("missing")) and item.get("active") and item.get("pane_visible") for item in rows),
    }


def tag_metrics(page: Page) -> dict[str, Any]:
    row = page.locator("#stockRows tr").first
    tags = row.locator(".tag-list > span")
    tops = [round(tags.nth(index).bounding_box()["y"], 1) for index in range(tags.count())]
    container = box_metrics(page, "#stockRows tr:first-child .tag-list")
    return {
        "count": tags.count(),
        "line_count": len(set(tops)),
        "tops": tops,
        "container": container,
        "max_tag_overflow": row.locator(".tag-list").evaluate(
            "element => Math.max(0, element.scrollWidth - element.clientWidth)"
        ),
    }


def board_layout_metrics(page: Page) -> dict[str, Any]:
    return page.evaluate(
        """() => {
            const headers = [...document.querySelectorAll('.stock-table thead th')]
                .map(item => item.textContent.trim())
                .filter(Boolean);
            const row = document.querySelector('#stockRows tr:first-child');
            const miniCell = row?.querySelector('.col-mini');
            const miniBox = miniCell?.querySelector('.mini-chart-box');
            const miniCard = miniCell?.querySelector('.board-mini-cell');
            const miniInfo = miniCell?.querySelector('.board-mini-info');
            const typeCell = row?.querySelector('.col-type');
            const inlinePanel = document.querySelector('.inline-watch-panel');
            const boardBody = document.querySelector('.board-body');
            const text = document.body.innerText || '';
            const chartRect = miniBox?.getBoundingClientRect();
            const cardRect = miniCard?.getBoundingClientRect();
            const infoRect = miniInfo?.getBoundingClientRect();
            return {
                headers,
                forbidden_headers: ['现价', '涨跌', '成交额', '分钟量能'].filter(item => headers.includes(item)),
                forbidden_text: ['easy_tdx历史逐笔成交', '本地按7只官方成分股聚合', '09:30-15:00'].filter(item => text.includes(item)),
                mini_cell_width: miniCell?.getBoundingClientRect().width || 0,
                mini_card_width: cardRect?.width || 0,
                mini_box_width: chartRect?.width || 0,
                mini_chart_width: chartRect?.width || 0,
                mini_chart_height: chartRect?.height || 0,
                mini_chart_ratio: cardRect?.width ? ((chartRect?.width || 0) / cardRect.width) : 0,
                mini_info_width: infoRect?.width || 0,
                mini_info_blocks: miniInfo?.querySelectorAll('.board-mini-metric').length || 0,
                trade_tape_count: miniInfo?.querySelectorAll('.trade-tape').length || 0,
                trade_tape_rows: miniInfo?.querySelectorAll('.trade-tape-row').length || 0,
                trade_tape_empty: miniInfo?.querySelectorAll('.trade-tape-empty').length || 0,
                type_width: typeCell?.getBoundingClientRect().width || 0,
                inline_watch_width: inlinePanel?.getBoundingClientRect().width || 0,
                board_body_width: boardBody?.getBoundingClientRect().width || 0,
            };
        }"""
    )


def add_page_error_handlers(
    page: Page,
    label: str,
    console_errors: list[str],
    page_errors: list[str],
) -> None:
    page.on(
        "console",
        lambda message: console_errors.append(f"{label}: {message.text}")
        if message.type == "error"
        else None,
    )
    page.on("pageerror", lambda error: page_errors.append(f"{label}: {error}"))


def wait_for_terminal(page: Page) -> None:
    page.goto(page.url or "http://127.0.0.1:8788/", wait_until="domcontentloaded", timeout=30_000)
    page.locator("#stockRows tr").first.wait_for(timeout=90_000)
    page.locator("#tapeChart canvas").first.wait_for(timeout=30_000)
    page.locator("#sectorFlowChart canvas").first.wait_for(timeout=30_000)
    page.wait_for_timeout(600)


def chart_call_counts(page: Page) -> dict[str, int]:
    return page.evaluate(
        """() => {
            const result = {};
            for (const [key, selector] of [['tape', '#tapeChart'], ['flow', '#sectorFlowChart'], ['detail', '#detailChart']]) {
                const chart = window.echarts?.getInstanceByDom(document.querySelector(selector));
                if (!chart) { result[key] = -1; continue; }
                if (!chart.__verifyOriginalSetOption) chart.__verifyOriginalSetOption = chart.setOption.bind(chart);
                let count = 0;
                chart.setOption = (...args) => { count += 1; return chart.__verifyOriginalSetOption(...args); };
                chart.__verifySetOptionCount = () => count;
                result[key] = 0;
            }
            return result;
        }"""
    )


def read_chart_call_counts(page: Page) -> dict[str, int]:
    return page.evaluate(
        """() => Object.fromEntries(
            [['tape', '#tapeChart'], ['flow', '#sectorFlowChart'], ['detail', '#detailChart']].map(([key, selector]) => {
                const chart = window.echarts?.getInstanceByDom(document.querySelector(selector));
                return [key, chart?.__verifySetOptionCount?.() ?? -1];
            })
        )"""
    )


def click_first_marker(page: Page) -> dict[str, Any]:
    marker = page.evaluate(
        """() => {
            const chart = window.echarts?.getInstanceByDom(document.querySelector('#detailChart'));
            if (!chart) return null;
            const option = chart.getOption();
            for (const series of option.series || []) {
                if (series.type === 'scatter' && series.data?.length) {
                    const data = series.data[0];
                    return { phase: data.marker?.phase || series.name, value: data.value, time: data.marker?.time || data.value?.[0] };
                }
            }
            return null;
        }"""
    )
    if not marker:
        return {"available": False}
    pixel = page.evaluate(
        """value => {
            const chart = window.echarts.getInstanceByDom(document.querySelector('#detailChart'));
            return chart.convertToPixel({xAxisIndex: 0, yAxisIndex: 0}, value);
        }""",
        marker["value"],
    )
    if (
        not isinstance(pixel, list)
        or len(pixel) < 2
        or not all(isinstance(value, (int, float)) and math.isfinite(value) for value in pixel[:2])
    ):
        return {
            "available": False,
            "reason": "marker coordinate is not finite",
            "phase": marker["phase"],
            "time": marker["time"],
        }
    box = page.locator("#detailChart").bounding_box()
    if not box:
        return {"available": False, "reason": "detail chart has no box"}
    page.mouse.move(box["x"] + pixel[0], box["y"] + pixel[1])
    page.wait_for_timeout(160)
    tooltip_text = page.locator("#detailChart").inner_text()
    page.mouse.click(box["x"] + pixel[0], box["y"] + pixel[1])
    page.wait_for_timeout(120)
    selected = page.locator("#detailSelectedPoint").inner_text()
    return {
        "available": True,
        "phase": marker["phase"],
        "time": marker["time"],
        "tooltip": tooltip_text,
        "tooltip_has_detail": marker["time"] in tooltip_text and "触发" in tooltip_text,
        "selected": selected,
        "updated": marker["time"] in selected and marker["phase"] in selected,
    }


def marker_visual_metrics(page: Page) -> dict[str, Any]:
    return page.evaluate(
        """() => {
            const chart = window.echarts?.getInstanceByDom(document.querySelector('#detailChart'));
            if (!chart) return { count: 0, missing: true };
            const series = (chart.getOption().series || []).filter(item => item.type === 'scatter');
            const confirmationSeries = series.filter(item => item.name === '公式买点' || item.name === '公式卖点');
            const goldSeries = series.filter(item => item.name === '买点共振');
            const buySeries = series.filter(item => item.name === '公式买点');
            const sellSeries = series.filter(item => item.name === '公式卖点');
            const points = confirmationSeries.flatMap(item => (item.data || []).map(data => ({
                name: item.name,
                time: data.marker?.time || data.value?.[0] || '',
                action: data.marker?.action || '',
                phase: data.marker?.phase || '',
            }))).sort((left, right) => left.time.localeCompare(right.time));
            const labelsVisible = confirmationSeries.some(item => item.label?.show === true)
                || confirmationSeries.some(item => (item.data || []).some(data => data.label?.show === true));
            const buyTimes = new Set(buySeries.flatMap(item => (item.data || []).map(data => data.marker?.time || data.value?.[0] || '')));
            const goldTimes = goldSeries.flatMap(item => (item.data || []).map(data => data.marker?.time || data.value?.[0] || ''));
            return {
                count: points.length,
                buy_count: buySeries.reduce((total, item) => total + (item.data || []).length, 0),
                sell_count: sellSeries.reduce((total, item) => total + (item.data || []).length, 0),
                gold_count: goldSeries.reduce((total, item) => total + (item.data || []).length, 0),
                names: [...new Set(confirmationSeries.map(item => item.name))],
                symbols: [...new Set(confirmationSeries.map(item => item.symbol))],
                colors: [...new Set(confirmationSeries.map(item => item.itemStyle?.color))],
                extra_scatter_names: [...new Set(series.map(item => item.name))]
                    .filter(name => !['公式买点', '公式卖点', '买点共振'].includes(name)),
                labels_visible: labelsVisible,
                points,
                gold_subset_of_buy: goldTimes.every(time => buyTimes.has(time)),
            };
        }"""
    )


def check_detail(page: Page, code: str | None = None) -> dict[str, Any]:
    rows = page.locator("#stockRows tr")
    target = page.locator(f'#stockRows tr[data-code="{code}"]') if code else page.locator("#stockRows tr").first
    if not target.count():
        target = rows.first
    target.scroll_into_view_if_needed()
    page.locator("#boardViewport").evaluate(
        "element => { element.scrollTop = Math.min(240, element.scrollHeight - element.clientHeight); }"
    )
    page.wait_for_timeout(100)
    page_scroll_before = page.evaluate("window.scrollY")
    board_scroll_before = page.locator("#boardViewport").evaluate("element => element.scrollTop")
    selected_code = target.get_attribute("data-code") or ""
    chart_call_counts(page)
    page.evaluate(
        """() => {
            const probe = { active: true, gaps: [], last: performance.now() };
            window.__detailFrameProbe = probe;
            const tick = now => {
                if (!probe.active) return;
                probe.gaps.push(now - probe.last);
                probe.last = now;
                requestAnimationFrame(tick);
            };
            requestAnimationFrame(tick);
        }"""
    )
    started = time.perf_counter()
    # Dispatch on the already selected row so Playwright does not introduce
    # an unrelated scroll while trying to bring an off-screen row into view.
    target.dispatch_event("click")
    page.locator("#detailModal").wait_for(state="visible", timeout=30_000)
    modal_open_ms = round((time.perf_counter() - started) * 1000)
    page.locator("#detailChart canvas").first.wait_for(timeout=30_000)
    page.wait_for_function(
        "document.querySelectorAll('#detailConfluence .confluence-block').length >= 4",
        timeout=15_000,
    )
    body_lock = page.evaluate(
        """() => ({
            position: document.body.style.position,
            overflow: getComputedStyle(document.body).overflow,
            top: Number.parseFloat(document.body.style.top || '0') || 0,
            scrollY: window.scrollY,
        })"""
    )
    title = page.locator("#detailTitle").inner_text()
    selected_point_before = page.locator("#detailSelectedPoint").inner_text()
    marker_visual = marker_visual_metrics(page)
    marker_result = click_first_marker(page)
    tabs = check_detail_tabs(page)
    detail_chart_box = box_metrics(page, "#detailChart")
    detail_chart_calls = read_chart_call_counts(page)
    frame_metrics = page.evaluate(
        """() => {
            const probe = window.__detailFrameProbe || { gaps: [] };
            probe.active = false;
            const gaps = probe.gaps || [];
            return {
                samples: gaps.length,
                max_gap_ms: gaps.length ? Math.max(...gaps) : 0,
                over_32ms: gaps.filter(value => value > 32).length,
                over_50ms: gaps.filter(value => value > 50).length,
            };
        }"""
    )
    page.locator("#detailCloseBtn").click()
    page.wait_for_timeout(120)
    return {
        "code": selected_code,
        "title": title,
        "modal_open_ms": modal_open_ms,
        "detail_canvas": canvas_metrics(page, "#detailChart"),
        "detail_chart_box": detail_chart_box,
        "selected_point_before": selected_point_before,
        "marker": marker_result,
        "marker_visual": marker_visual,
        "tabs": tabs,
        "chart_calls_while_open": detail_chart_calls,
        "frame_metrics": frame_metrics,
        "board_scroll_before": board_scroll_before,
        "board_scroll_after": page.locator("#boardViewport").evaluate("element => element.scrollTop"),
        "page_scroll_before": page_scroll_before,
        "page_scroll_during": body_lock["scrollY"],
        "page_scroll_after": page.evaluate("window.scrollY"),
        "body_lock": body_lock,
        "marker_cards": page.locator("#detailMarkers").count(),
    }


def check_pending_preview_race(page: Page) -> dict[str, Any]:
    rows = page.locator("#stockRows tr[data-code]")
    if rows.count() < 2:
        return {"available": False, "reason": "not enough stock rows"}
    preview_code = rows.first.get_attribute("data-code") or ""
    target = rows.nth(min(5, rows.count() - 1))
    target_code = target.get_attribute("data-code") or ""
    started = time.perf_counter()
    page.evaluate(
        """([previewCode, targetCode]) => {
            state.detailCache.clear();
            state.detail = null;
            state.selectedCode = previewCode;
            schedulePreview(previewCode);
            document.querySelector(`#stockRows tr[data-code="${targetCode}"]`)
                ?.dispatchEvent(new MouseEvent('click', { bubbles: true }));
        }""",
        [preview_code, target_code],
    )
    page.locator("#detailModal").wait_for(state="visible", timeout=10_000)
    page.wait_for_function(
        "code => document.querySelector('#detailTitle')?.textContent.includes(code) && !document.querySelector('#detailTitle')?.textContent.includes('加载中')",
        arg=target_code,
        timeout=20_000,
    )
    elapsed_ms = round((time.perf_counter() - started) * 1000)
    title = page.locator("#detailTitle").inner_text()
    page.locator("#detailCloseBtn").click()
    page.wait_for_timeout(100)
    return {
        "available": True,
        "preview_code": preview_code,
        "target_code": target_code,
        "title": title,
        "loaded": target_code in title and "加载中" not in title,
        "elapsed_ms": elapsed_ms,
    }


def check_sector_switch(page: Page) -> dict[str, Any]:
    page.evaluate("window.scrollTo(0, 180)")
    before = page.evaluate("window.scrollY")
    sector = page.locator("#sectors .sector-row").first
    sector_name = sector.get_attribute("data-sector") or ""
    # The application itself restores the page offset after the async fetch;
    # dispatching avoids the test runner scrolling the sector row first.
    sector.dispatch_event("click")
    page.wait_for_function(
        "name => document.querySelector('#boardTitle')?.textContent.includes(name)",
        arg=sector_name,
        timeout=30_000,
    )
    page.wait_for_timeout(250)
    after = page.evaluate("window.scrollY")
    selected_title = page.locator("#boardTitle").inner_text()
    page.locator("#clearSectorBtn").dispatch_event("click")
    page.wait_for_function(
        "() => document.querySelector('#boardTitle')?.textContent.includes('全市场')",
        timeout=30_000,
    )
    return {
        "sector": sector_name,
        "title_after_select": selected_title,
        "scroll_before": before,
        "scroll_after": after,
        "preserved_scroll": abs(after - before) <= 3,
    }


def check_page(
    browser: Browser,
    *,
    label: str,
    viewport: dict[str, int],
    base_url: str,
    screenshot_dir: Path,
    console_errors: list[str],
    page_errors: list[str],
    check_detail_modal: bool,
) -> dict[str, Any]:
    page = browser.new_page(viewport=viewport, device_scale_factor=1)
    page.goto(base_url, wait_until="domcontentloaded", timeout=30_000)
    add_page_error_handlers(page, label, console_errors, page_errors)
    page.locator("#stockRows tr").first.wait_for(timeout=90_000)
    page.locator("#tapeChart canvas").first.wait_for(timeout=30_000)
    page.locator("#sectorFlowChart canvas").first.wait_for(timeout=30_000)
    # Let the initial preview and the first websocket payload settle before
    # measuring the no-redraw behavior of a frozen close snapshot.
    page.wait_for_timeout(2_500)

    result: dict[str, Any] = {
        "row_count": page.locator("#stockRows tr").count(),
        "opening_elements": page.locator("#openingStage, #openingCandidates, .opening-panel").count(),
        "document": document_metrics(page),
        "focus_grid": box_metrics(page, ".focus-grid"),
        "tape": box_metrics(page, "#tapeChart"),
        "flow": box_metrics(page, "#sectorFlowChart"),
        "board": box_metrics(page, "#boardViewport"),
        "board_layout": board_layout_metrics(page),
        "tags": tag_metrics(page),
        "radar": box_metrics(page, "#resonanceRadar"),
        "radar_cards": page.locator("#resonanceRadar .radar-card").count(),
        "mini_charts": mini_chart_metrics(page),
        "flow_chart": canvas_metrics(page, "#sectorFlowChart"),
        "tape_chart": canvas_metrics(page, "#tapeChart"),
        "frozen": "冻结" in page.locator("#freezeState").inner_text(),
        "clock": page.locator("#pageClock").inner_text(),
    }
    result["chart_order"] = {
        "tape_y": result["tape"].get("y", -1),
        "flow_y": result["flow"].get("y", -1),
        "tape_above_flow": result["tape"].get("y", -1) < result["flow"].get("y", -1),
    }

    screenshot_dir.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(screenshot_dir / f"terminal-{label}.png"), full_page=True)

    if result["frozen"]:
        chart_call_counts(page)
        before_clock = result["clock"]
        page.wait_for_timeout(6_500)
        result["frozen_refresh_calls"] = read_chart_call_counts(page)
        result["clock_after_wait"] = page.locator("#pageClock").inner_text()
        result["frozen_stable"] = (
            result["frozen_refresh_calls"]["tape"] == 0
            and result["frozen_refresh_calls"]["flow"] == 0
            and result["clock_after_wait"] == before_clock
        )

    if check_detail_modal and result["row_count"]:
        result["sector_switch"] = check_sector_switch(page)
        # Restore the first page after sector switching before opening detail.
        target_code = "300476" if page.locator('#stockRows tr[data-code="300476"]').count() else None
        result["detail"] = check_detail(page, target_code)
        result["pending_preview_race"] = check_pending_preview_race(page)

    page.close()
    return result


def collect_failures(results: dict[str, Any], console_errors: list[str], page_errors: list[str]) -> list[str]:
    failures = [*console_errors, *page_errors]
    for label, result in results.items():
        if result["row_count"] <= 0:
            failures.append(f"{label}: stock board is empty")
        if result["opening_elements"]:
            failures.append(f"{label}: opening decision UI is still rendered")
        if result["document"]["overflow_x"] > 2:
            failures.append(f"{label}: document overflows horizontally by {result['document']['overflow_x']}px")
        for chart_name in ("flow_chart", "tape_chart"):
            chart = result[chart_name]
            if chart["count"] <= 0 or max(chart["data_lengths"], default=0) < 2_000:
                failures.append(f"{label}: {chart_name} canvas is missing or blank")
        if label == "mobile" and not result["chart_order"]["tape_above_flow"]:
            failures.append(f"{label}: intraday tape is not above sector flow chart")
        if result.get("radar_cards", 0) < 4:
            failures.append(f"{label}: resonance radar cards are missing")
        mini = result.get("mini_charts", {})
        if mini.get("count", 0) <= 0 or mini.get("usable_count", 0) <= 0:
            failures.append(f"{label}: mini intraday charts are missing or empty")
        if label in {"desktop", "wide"} and mini.get("board_count", 0) <= 0:
            failures.append(f"{label}: stock board mini charts are missing")
        if result["tags"]["max_tag_overflow"] > 2:
            failures.append(f"{label}: type tags overflow their column")
        layout = result.get("board_layout", {})
        if layout.get("forbidden_headers"):
            failures.append(f"{label}: removed quote columns are still visible: {layout['forbidden_headers']}")
        if layout.get("forbidden_text"):
            failures.append(f"{label}: internal source text is still visible: {layout['forbidden_text']}")
        if label in {"desktop", "wide"}:
            if layout.get("mini_card_width", 0) < 340:
                failures.append(f"{label}: stock mini/flow cell is too narrow")
            if not 0.28 <= layout.get("mini_chart_ratio", 0) <= 0.48:
                failures.append(f"{label}: stock mini chart is not using the compact half-width layout")
            if layout.get("mini_chart_width", 0) < 90 or layout.get("mini_chart_height", 0) < 64:
                failures.append(f"{label}: stock mini chart is too small to read")
            if layout.get("mini_info_blocks", 0) > 0:
                failures.append(f"{label}: old stock mini metric blocks are still visible")
            if layout.get("mini_info_width", 0) < 180 or layout.get("trade_tape_count", 0) <= 0:
                failures.append(f"{label}: stock recent transaction tape is missing")
            if layout.get("type_width", 0) > 112:
                failures.append(f"{label}: type column is too wide")
            body_width = layout.get("board_body_width", 0) or 1
            if layout.get("inline_watch_width", 0) / body_width < 0.34:
                failures.append(f"{label}: inline watchlist panel is still too narrow")
    detail = results.get("desktop", {}).get("detail", {})
    if detail:
        if not detail.get("title"):
            failures.append("desktop: detail modal has no title")
        if detail.get("modal_open_ms", 99_999) > 5_000:
            failures.append("desktop: detail modal did not open promptly")
        if detail.get("marker_cards", 0):
            failures.append("desktop: duplicate marker card list is still rendered")
        if detail.get("detail_chart_box", {}).get("overflowX", 0) > 2:
            failures.append("desktop: warmed detail canvas overflows its visible chart container")
        marker = detail.get("marker", {})
        if marker.get("available") and not marker.get("updated"):
            failures.append("desktop: clicking a chart marker did not update the selected-point bar")
        if marker.get("available") and not marker.get("tooltip_has_detail"):
            failures.append("desktop: hovering a formula marker did not show its trigger tooltip")
        visual = detail.get("marker_visual", {})
        if visual.get("labels_visible"):
            failures.append("desktop: formula markers still render chart labels")
        if visual.get("extra_scatter_names"):
            failures.append("desktop: unexpected marker series is still visible")
        if set(visual.get("symbols", [])) - {"circle"}:
            failures.append("desktop: formula markers are not plain circles")
        if set(visual.get("colors", [])) - {"#f6465d", "#2ebd85"}:
            failures.append("desktop: formula markers use colors other than red/green")
        if visual.get("gold_count", 0) and not visual.get("gold_subset_of_buy", True):
            failures.append("desktop: gold resonance markers are not attached to buy markers")
        chart_calls = detail.get("chart_calls_while_open", {})
        if chart_calls.get("tape", 0) > 0:
            failures.append("desktop: hidden main intraday chart redrew while detail modal was open")
        if chart_calls.get("detail", 0) > 3:
            failures.append("desktop: detail chart redrew more than the loading/fast/full stages")
        body_lock = detail.get("body_lock", {})
        if body_lock.get("position") == "fixed":
            failures.append("desktop: detail modal still forces a full-page fixed-position layout")
        if body_lock.get("overflow") != "hidden" or abs(body_lock.get("scrollY", 0) - detail["page_scroll_before"]) > 2:
            failures.append("desktop: detail modal did not lock scrolling at the current page offset")
        if abs(detail["page_scroll_after"] - detail["page_scroll_before"]) > 2:
            failures.append("desktop: closing detail modal changed the page scroll position")
        if abs(detail["board_scroll_after"] - detail["board_scroll_before"]) > 2:
            failures.append("desktop: opening/closing detail changed the board scroll position")
        tabs = detail.get("tabs", {})
        if tabs.get("initial_active") != "messages":
            failures.append("desktop: detail default tab is not 星球/messages")
        if not tabs.get("all_usable"):
            failures.append("desktop: detail tabs are not all usable")
        sector = results.get("desktop", {}).get("sector_switch", {})
        if sector and not sector.get("preserved_scroll"):
            failures.append("desktop: switching sector changed the page scroll position")
        preview_race = results.get("desktop", {}).get("pending_preview_race", {})
        if preview_race.get("available") and not preview_race.get("loaded"):
            failures.append("desktop: pending main-chart preview cancelled an uncached detail request")
    if results.get("desktop", {}).get("frozen") and not results["desktop"].get("frozen_stable"):
        failures.append("desktop: frozen close snapshot continued to redraw")
    return failures


def main() -> int:
    args = parse_args()
    args.screenshot_dir.mkdir(parents=True, exist_ok=True)
    console_errors: list[str] = []
    page_errors: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        results = {
            "desktop": check_page(
                browser,
                label="desktop",
                viewport={"width": 1440, "height": 900},
                base_url=args.base_url,
                screenshot_dir=args.screenshot_dir,
                console_errors=console_errors,
                page_errors=page_errors,
                check_detail_modal=True,
            ),
            "wide": check_page(
                browser,
                label="wide",
                viewport={"width": 1920, "height": 1080},
                base_url=args.base_url,
                screenshot_dir=args.screenshot_dir,
                console_errors=console_errors,
                page_errors=page_errors,
                check_detail_modal=False,
            ),
            "mobile": check_page(
                browser,
                label="mobile",
                viewport={"width": 390, "height": 844},
                base_url=args.base_url,
                screenshot_dir=args.screenshot_dir,
                console_errors=console_errors,
                page_errors=page_errors,
                check_detail_modal=False,
            ),
        }
        browser.close()
    failures = collect_failures(results, console_errors, page_errors)
    payload = {
        "base_url": args.base_url,
        "results": results,
        "console_errors": console_errors,
        "page_errors": page_errors,
        "failures": failures,
        "screenshots": [str(path) for path in sorted(args.screenshot_dir.glob("terminal-*.png"))],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

