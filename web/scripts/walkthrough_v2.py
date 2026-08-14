# 走查新布局：截图 + 关键断言
import asyncio, sys
from playwright.async_api import async_playwright

OUT = r"G:\ai\lh\9\data\runtime\layout-v2-check.png"

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={"width": 1920, "height": 1080})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        await page.goto("http://127.0.0.1:7100/", wait_until="networkidle", timeout=45000)
        await page.wait_for_timeout(6000)

        # 断言关键板块都渲染了
        checks = {
            "指数共振图": await page.locator("text=指数共振").count(),
            "净流入榜": await page.locator("text=板块资金净流入").count(),
            "动能轨迹图": await page.locator("text=板块资金动能").count(),
            "涨停梯队图": await page.locator("text=涨停情绪梯队").count(),
            "大单列表头": await page.locator("th:has-text('大单')").count(),
            "搜索框在顶栏": await page.locator("header input").count(),
            "机会队列": await page.locator("text=机会队列").count(),
        }
        for k, v in checks.items():
            print(f"{k}: {'OK' if v > 0 else 'MISSING'}")

        # echarts canvas 数量（4 张图）
        canvases = await page.locator("main canvas").count()
        print(f"canvas count: {canvases}")

        # 切换板块测试不闪屏：记录 tbody 行数，点击一个板块，立即检查行未清空
        rows_before = await page.locator("tbody tr").count()
        sector_btn = page.locator("section:has-text('板块强弱') button").nth(3)
        await sector_btn.click()
        await page.wait_for_timeout(300)
        rows_mid = await page.locator("tbody tr").count()
        print(f"切换板块: 前行数={rows_before} 点击后瞬间={rows_mid} (应保持>0)")
        # 领涨锚是否出现
        await page.wait_for_timeout(4000)
        anchor = await page.locator("text=领涨锚").count()
        print(f"领涨锚: {'OK' if anchor > 0 else '未出现(该板块可能无龙头)'}")
        # 滞涨标记
        stag = await page.locator("text=滞涨").count()
        print(f"滞涨标记数: {stag}")

        await page.screenshot(path=OUT, full_page=False)
        print("page errors:", errors if errors else "none")
        await browser.close()

asyncio.run(main())
