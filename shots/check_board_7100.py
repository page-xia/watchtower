# -*- coding: utf-8 -*-
"""打开盯盘前端，检查活跃股榜单是否渲染数据，抓取控制台错误。"""
import sys
from playwright.sync_api import sync_playwright

URL = "http://localhost:7100/"

console_msgs = []
page_errors = []

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1680, "height": 950})
    page.on("console", lambda m: console_msgs.append(f"{m.type}: {m.text[:200]}"))
    page.on("pageerror", lambda e: page_errors.append(str(e)[:300]))
    page.goto(URL)
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(6000)  # 等 WS 快照 + 增量到达

    rows = page.locator("table tbody tr").count()
    print("table rows:", rows)
    # 榜单面板文本采样
    board_text = page.locator("section", has_text="活跃股榜单").first
    if board_text.count():
        txt = board_text.inner_text()
        print("board panel text head:", txt[:400].replace("\n", " | "))
    # 做T分析列内容采样
    tds = page.locator("td", has_text="资").all()
    print("t_analysis cells:", len(tds))
    for td in tds[:3]:
        print("  cell:", td.inner_text().replace("\n", " | "))
    # 顶部连接状态
    body_head = page.locator("body").inner_text()[:200].replace("\n", " | ")
    print("body head:", body_head)
    page.screenshot(path="G:/ai/lh/9/shots/check_board_7100.png", full_page=False)
    browser.close()

print("--- console ---")
for m in console_msgs[:20]:
    print(m)
print("--- page errors ---")
for e in page_errors[:10]:
    print(e)
