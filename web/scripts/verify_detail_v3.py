# 验证：详情页新布局 + ECharts 报错已消失
import asyncio
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={"width": 1920, "height": 1080})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        await page.goto("http://127.0.0.1:7100/", wait_until="networkidle", timeout=45000)
        await page.wait_for_timeout(5000)

        rows = page.locator("tbody tr")
        for i in range(3):
            await rows.nth(i).click()
            await page.wait_for_timeout(2500)
            chart = page.locator(".terminal-panel canvas").first
            box = await chart.bounding_box()
            if box:
                for frac in (0.2, 0.4, 0.6, 0.8):
                    await page.mouse.move(box["x"] + box["width"] * frac, box["y"] + box["height"] * 0.35)
                    await page.wait_for_timeout(250)
            if i == 0:
                await page.screenshot(path=r"G:\ai\lh\9\data\runtime\detail-layout-v3.png", full_page=False)
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(600)
            print(f"stock {i}: errors so far = {len(errors)}")

        # 停留等两个轮询周期确认增量更新不再抛异常
        await rows.nth(1).click()
        await page.wait_for_timeout(22000)
        print(f"after 22s polling: errors = {len(errors)}")
        for e in errors[:5]:
            print("PAGEERROR:", e[:200])
        await browser.close()

asyncio.run(main())
