# 压测详情页：悬停 tooltip + 轮询周期 + 连续切换个股，抓运行时错误
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
        n = min(await rows.count(), 6)
        for i in range(n):
            await rows.nth(i).click()
            await page.wait_for_timeout(2500)
            # 悬停分时图多个位置触发 tooltip
            chart = page.locator(".terminal-panel canvas").first
            box = await chart.bounding_box()
            if box:
                for frac in (0.15, 0.35, 0.55, 0.75, 0.9):
                    await page.mouse.move(box["x"] + box["width"] * frac, box["y"] + box["height"] * 0.4)
                    await page.wait_for_timeout(200)
            await page.wait_for_timeout(1800)
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(600)
            print(f"stock {i}: errors so far = {len(errors)}")

        # 停留一个详情等两个 10s 轮询周期
        await rows.nth(0).click()
        await page.wait_for_timeout(22000)
        print(f"after 22s polling: errors = {len(errors)}")
        await page.screenshot(path=r"G:\ai\lh\9\data\runtime\detail-stress.png", full_page=False)
        for e in errors[:8]:
            print("PAGEERROR:", e[:400])
        await browser.close()

asyncio.run(main())
