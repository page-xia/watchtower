# 对比右边缘：悬停前/悬停中/移开后 三个状态的高清裁剪
import asyncio
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={"width": 1920, "height": 1080})
        await page.goto("http://127.0.0.1:7100/", wait_until="networkidle", timeout=45000)
        await page.wait_for_timeout(5000)
        await page.locator("tbody tr").first.click()
        await page.wait_for_timeout(4000)
        chart = page.locator(".fixed.inset-0 canvas").first
        box = await chart.bounding_box()
        # 右边缘裁剪区域（绝对坐标）
        rx = box["x"] + box["width"] - 260
        clip = {"x": rx, "y": box["y"], "width": 260, "height": box["height"] * 0.66}

        await page.screenshot(path=r"G:\ai\lh\9\data\runtime\edge-before.png", clip=clip)
        # 悬停在中部（tooltip 不挡右边缘）
        await page.mouse.move(box["x"] + box["width"] * 0.35, box["y"] + box["height"] * 0.3)
        await page.wait_for_timeout(500)
        await page.screenshot(path=r"G:\ai\lh\9\data\runtime\edge-hover-mid.png", clip=clip)
        # 悬停在右边缘
        await page.mouse.move(box["x"] + box["width"] * 0.92, box["y"] + box["height"] * 0.3)
        await page.wait_for_timeout(500)
        await page.screenshot(path=r"G:\ai\lh\9\data\runtime\edge-hover-right.png", clip=clip)
        # 移开
        await page.mouse.move(box["x"] + 10, box["y"] + box["height"] + 40)
        await page.wait_for_timeout(500)
        await page.screenshot(path=r"G:\ai\lh\9\data\runtime\edge-after.png", clip=clip)
        print("done")
        await browser.close()

asyncio.run(main())
