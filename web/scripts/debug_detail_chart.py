# 复现个股详情分时图渲染问题
import asyncio
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={"width": 1920, "height": 1080})
        console = []
        errors = []
        page.on("console", lambda m: console.append(f"{m.type}: {m.text}") if m.type in ("error", "warning") else None)
        page.on("pageerror", lambda e: errors.append(str(e)))
        await page.goto("http://127.0.0.1:7100/", wait_until="networkidle", timeout=45000)
        await page.wait_for_timeout(5000)
        # 点击第一行个股打开详情
        await page.locator("tbody tr").first.click()
        await page.wait_for_timeout(6000)
        await page.screenshot(path=r"G:\ai\lh\9\data\runtime\detail-chart-debug.png", full_page=False)
        # 详情里 canvas 情况
        n = await page.locator(".terminal-panel canvas").count()
        print("detail canvases:", n)
        chart_box = await page.locator("text=分时数据加载中").count()
        print("still loading text:", chart_box)
        print("--- console ---")
        for c in console[:20]:
            print(c[:300])
        print("--- page errors ---")
        for e in errors[:10]:
            print(e[:500])
        await browser.close()

asyncio.run(main())
