"""デモ画面のスクリーンショットを docs/images/ に撮る（scripts/demo.py で起動した管理画面を使う）。

    python3 scripts/demo.py --fresh &     # 先にデモを起動しておく
    pip install playwright && playwright install chromium   # 初回だけ
    python3 scripts/screenshots.py

入っている Chrome を使うなら、Chromium を取ってこなくてよい: PLAYWRIGHT_CHANNEL=chrome python3 scripts/screenshots.py

画面には、デモを置いた場所と hootl-sdd の場所が写る（設定・新しいアプリ）。公開する画像を撮るときは、
自分の名前が入らない場所（例: /tmp/hootl-sdd に clone して、そこから demo.py を起動する）で撮ること。
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "images"
BASE = "http://127.0.0.1:8799"
TOKEN = "demo-token-0123456789abcdef"


def login(page: Page) -> None:
    page.goto(BASE + "/login")
    page.fill("input[name=token]", TOKEN)
    page.press("input[name=token]", "Enter")
    page.wait_for_load_state("networkidle")


def project(page: Page, name: str) -> None:
    page.click(f".projects button:has-text('{name}')")
    time.sleep(1)


def tab(page: Page, name: str) -> None:
    page.click(f"button[data-tab={name}]")
    time.sleep(1.2)


def shot(page: Page, name: str, full: bool = True) -> None:
    path = OUT / f"{name}.png"
    page.screenshot(path=str(path), full_page=full)
    print(f"  {path.relative_to(ROOT)}")


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(channel=os.environ.get("PLAYWRIGHT_CHANNEL") or None)
        page = browser.new_page(viewport={"width": 1200, "height": 900})
        page.on("pageerror", lambda e: errors.append(str(e)))
        login(page)
        project(page, "quiz-demo")
        tab(page, "tasks")
        shot(page, "01-tasks")
        page.click(".row:has-text('T-005')")
        time.sleep(1.2)
        shot(page, "02-task-detail", full=False)
        page.click("#sheet-close")
        tab(page, "spec")
        page.click("text=開いて直す")
        time.sleep(1)
        shot(page, "03-spec")
        tab(page, "ideas")
        shot(page, "10-ideas")
        tab(page, "plan")
        shot(page, "04-plan")
        tab(page, "reports")
        page.click(".report summary")
        time.sleep(.5)
        shot(page, "05-reports")
        project(page, "kakeibo")
        tab(page, "settings")
        shot(page, "06-settings")
        page.click("text=＋ 新しいアプリ")
        time.sleep(1.5)
        page.fill("textarea", "家計簿。レシートの写真から金額を読んで、月ごとに費目別の合計を出したい。")
        page.fill("input[placeholder=kakeibo]", "kakeibo2")
        page.fill("input[placeholder=家計簿]", "家計簿")
        page.click("text=E2E テスト（Playwright）")
        time.sleep(.3)
        shot(page, "07-new-app")
        page.locator("#creations").scroll_into_view_if_needed()
        time.sleep(.3)
        page.locator("#creations").screenshot(path=str(OUT / "08-new-app-progress.png"))
        print("  docs/images/08-new-app-progress.png")

        phone = browser.new_page(viewport={"width": 390, "height": 844}, device_scale_factor=2)
        phone.on("pageerror", lambda e: errors.append(str(e)))
        login(phone)
        project(phone, "quiz-demo")
        tab(phone, "tasks")
        shot(phone, "09-phone-tasks", full=False)
        browser.close()
    if errors:
        print("画面のエラー:", *errors, sep="\n  ", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
