# 抓取分时图 tooltip 报错的完整堆栈
import asyncio
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={"width": 1920, "height": 1080})
        stacks = []
        page.on("pageerror", lambda e: stacks.append(e.stack or str(e)))
        await page.goto("http://127.0.0.1:7100/", wait_until="networkidle", timeout=45000)
        await page.wait_for_timeout(5000)
        await page.locator("tbody tr").first.click()
        await page.wait_for_timeout(3000)
        chart = page.locator(".terminal-panel canvas").first
        box = await chart.bounding_box()
        for frac in (0.2, 0.4, 0.6, 0.8):
            await page.mouse.move(box["x"] + box["width"] * frac, box["y"] + box["height"] * 0.35)
            await page.wait_for_timeout(300)
        print(f"errors: {len(stacks)}")
        if stacks:
            print(stacks[0][:2500])
        await browser.close()

asyncio.run(main())
