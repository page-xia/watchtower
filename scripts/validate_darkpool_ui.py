"""暗盘资金重构 UI 自检：首页面板 + 板块联动 + 详情页筹码/暗盘分区。

配合 webapp-testing 的 with_server.py 使用（后端 8899 + 前端 5199 由它托管）。
截图落在 shots/ 目录。
"""

from __future__ import annotations

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
SHOTS = ROOT / "shots"
BASE = "http://127.0.0.1:5199"


def main() -> int:
    SHOTS.mkdir(exist_ok=True)
    errors: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1720, "height": 980})
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.goto(BASE)
        page.wait_for_load_state("networkidle", timeout=30000)

        # 1. 首页暗盘资金面板（等东财快照落地，板块桶按钮出现）
        page.wait_for_selector("text=暗盘资金", timeout=30000)
        try:
            page.wait_for_selector('button[title*="点击联动"]', timeout=90000)
        except Exception:
            print("WARN: 东财板块桶未等到（可能快照未落地），先截当前状态")
        page.wait_for_timeout(1500)
        page.screenshot(path=str(SHOTS / "darkpool_home.png"), full_page=False)
        print("home ok")

        # 2. 板块联动：点击盘中资金地图第一个板块桶 → 头部应出现「联动:」chip
        bucket = page.locator('button[title*="点击联动"]').first
        bucket_name = (bucket.inner_text() or "").split("\n")[0].strip()
        bucket.click()
        page.wait_for_timeout(2500)
        linked = page.locator("text=联动:").count()
        print(f"linkage click: {bucket_name} -> 联动 chip count={linked}")
        page.screenshot(path=str(SHOTS / "darkpool_linked.png"), full_page=False)

        # 清除联动（点 chip 的 ✕）
        if linked:
            page.locator("button:has-text('联动:')").first.click()
            page.wait_for_timeout(1200)

        # 3. 个股详情：搜索 300308 打开详情，筹码 tab 默认，下方应为暗盘资金
        page.get_by_placeholder("代码 / 名称").fill("300308")
        page.wait_for_timeout(1200)
        page.locator("button:has-text('中际旭创')").first.click()
        page.wait_for_selector("text=暗盘资金", timeout=20000)
        page.wait_for_timeout(6000)  # 等筹码 + 暗盘资金数据就位
        page.screenshot(path=str(SHOTS / "darkpool_detail.png"), full_page=False)
        print("detail ok")

        browser.close()
    print("console errors:", len(errors))
    for e in errors[:5]:
        print("  console-error:", e[:160])
    return 0


if __name__ == "__main__":
    sys.exit(main())
