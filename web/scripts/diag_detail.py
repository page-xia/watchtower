# 诊断：打开详情并截图，确认分时图区域状态
import asyncio
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={"width": 1920, "height": 1080})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        await page.goto("http://127.0.0.1:7100/", wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(8000)
        n = await page.locator("tbody tr").count()
        print("rows:", n)
        await page.locator("tbody tr").first.click()
        await page.wait_for_timeout(8000)
        canvases = await page.locator(".fixed.inset-0 canvas").count()
        print("detail canvases:", canvases)
        loading = await page.locator("text=分时数据加载中").count()
        print("loading text:", loading)
        await page.screenshot(path=r"G:\ai\lh\9\data\runtime\edge-diag.png")
        print("errors:", errors if errors else "none")
        await browser.close()

asyncio.run(main())
