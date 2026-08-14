# 首页四个图悬停验证：悬停前后对比 + 报错捕获
import asyncio
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={"width": 1920, "height": 1080})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        await page.goto("http://127.0.0.1:7100/", wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(16000)

        charts = page.locator("main canvas")
        n = await charts.count()
        print("main canvases:", n)
        for i in range(n):
            c = charts.nth(i)
            box = await c.bounding_box()
            if not box or box["width"] < 50:
                continue
            # 悬停前
            await page.mouse.move(10, 10)
            await page.wait_for_timeout(300)
            await page.screenshot(path=rf"G:\ai\lh\9\data\runtime\main-chart{i}-before.png",
                                  clip={"x": box["x"]-8, "y": box["y"]-30, "width": box["width"]+16, "height": box["height"]+34})
            # 悬停中部
            await page.mouse.move(box["x"] + box["width"] * 0.5, box["y"] + box["height"] * 0.4)
            await page.wait_for_timeout(500)
            await page.screenshot(path=rf"G:\ai\lh\9\data\runtime\main-chart{i}-hover.png",
                                  clip={"x": box["x"]-8, "y": box["y"]-30, "width": box["width"]+16, "height": box["height"]+34})
            print(f"chart {i}: hovered")
        print("errors:", errors if errors else "none")
        await browser.close()

asyncio.run(main())
