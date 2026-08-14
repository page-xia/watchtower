# 验证板块资金动能修复：悬浮颜色一致 + 右端板块名 + 右列收窄
# 自起临时 vite dev server，截图后关闭进程树，不残留。
import asyncio
import os
import subprocess
import sys
import time
import urllib.request

ROOT = r"G:\ai\lh\9"
WEB = os.path.join(ROOT, "web")
PORT = 7102
OUT_FULL = os.path.join(ROOT, "data", "runtime", "sector-flow-fixed-full.png")
OUT_PANEL = os.path.join(ROOT, "data", "runtime", "sector-flow-fixed-panel.png")
NPM = r"G:\KimiData\daimon-share\daimon\command-process-owner\bin\npm.cmd"


def wait_ready(url: str, timeout: float = 60) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(1)
    return False


async def shoot() -> None:
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={"width": 1920, "height": 1080})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        await page.goto(f"http://127.0.0.1:{PORT}/", wait_until="networkidle", timeout=60000)
        await page.wait_for_timeout(8000)

        panel = page.locator("section:has-text('板块资金动能')").first
        box = await panel.bounding_box()
        print("panel box:", box)

        # 悬浮到动能图左中位置触发 tooltip
        canvas = panel.locator("canvas").first
        cbox = await canvas.bounding_box()
        if cbox:
            await page.mouse.move(cbox["x"] + cbox["width"] * 0.25, cbox["y"] + cbox["height"] * 0.5)
            await page.wait_for_timeout(800)

        await page.screenshot(path=OUT_FULL)
        if box:
            clip = {
                "x": max(box["x"] - 20, 0),
                "y": max(box["y"] - 10, 0),
                "width": box["width"] + 40,
                "height": box["height"] + 20,
            }
            await page.screenshot(path=OUT_PANEL, clip=clip)
        print("page errors:", errors or "none")
        await browser.close()


def main() -> int:
    proc = subprocess.Popen(
        [NPM, "run", "dev", "--", "--port", str(PORT), "--strictPort", "--host", "127.0.0.1"],
        cwd=WEB,
        stdout=open(os.path.join(ROOT, "data", "runtime", "vite-verify.log"), "w"),
        stderr=subprocess.STDOUT,
    )
    try:
        if not wait_ready(f"http://127.0.0.1:{PORT}/"):
            print("vite not ready")
            return 1
        asyncio.run(shoot())
        return 0
    finally:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
        print("vite stopped")


if __name__ == "__main__":
    sys.exit(main())
