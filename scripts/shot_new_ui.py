"""Screenshot the new React terminal UI for visual verification."""
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:7100"
OUT = Path("data/runtime/ui-check")
OUT.mkdir(parents=True, exist_ok=True)

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1600, "height": 900})
    errors: list[str] = []
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(BASE, wait_until="domcontentloaded", timeout=30_000)
    try:
        page.locator("tbody tr").first.wait_for(timeout=90_000)
    except Exception as e:
        print("WARN: no table rows:", e)
    page.wait_for_timeout(2500)
    page.screenshot(path=str(OUT / "new-main.png"))
    # open detail modal
    try:
        page.locator("tbody tr").first.click()
        page.wait_for_timeout(3500)
        page.screenshot(path=str(OUT / "new-detail.png"))
        page.keyboard.press("Escape")
        page.wait_for_timeout(500)
    except Exception as e:
        print("WARN: detail modal:", e)
    print("console_errors:", errors[:10])
    browser.close()
print("done")
