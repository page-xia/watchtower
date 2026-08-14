# 复现悬停分时图消失：悬停瞬间截图 + DOM 状态
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
        await page.locator("tbody tr").first.click()
        await page.wait_for_timeout(4000)

        chart = page.locator(".fixed.inset-0 canvas").first
        box = await chart.bounding_box()
        print("chart box:", box)
        # 悬停前截图
        await page.screenshot(path=r"G:\ai\lh\9\data\runtime\hover-before.png")
        # 悬停
        await page.mouse.move(box["x"] + box["width"] * 0.5, box["y"] + box["height"] * 0.3)
        await page.wait_for_timeout(600)
        await page.screenshot(path=r"G:\ai\lh\9\data\runtime\hover-during.png")
        # tooltip DOM
        tips = await page.locator(".fixed.inset-0 [class*='tooltip'], .fixed.inset-0 div[style*='pointer-events']").count()
        print("tooltip-ish nodes:", tips)
        # canvas 可见性
        vis = await chart.evaluate("el => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el); return { w: r.width, h: r.height, display: s.display, visibility: s.visibility, opacity: s.opacity } }")
        print("canvas state during hover:", vis)
        await page.mouse.move(box["x"] + box["width"] * 0.5, box["y"] + 5)
        await page.wait_for_timeout(400)
        await page.screenshot(path=r"G:\ai\lh\9\data\runtime\hover-after.png")
        print("errors:", errors if errors else "none")
        await browser.close()

asyncio.run(main())
