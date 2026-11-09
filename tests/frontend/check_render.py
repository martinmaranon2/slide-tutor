"""Playwright check of static/common.js renderMarkdown using tests/frontend/render_test.html.

Run: uv run python tests/frontend/check_render.py
"""
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
SHOT = Path(__file__).resolve().parents[1] / "output" / "render_test.png"


def main() -> int:
    failures: list[str] = []

    def check(cond: bool, msg: str) -> None:
        print(("ok   " if cond else "FAIL ") + msg)
        if not cond:
            failures.append(msg)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 900, "height": 1000})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.goto((HERE / "render_test.html").as_uri())
        page.wait_for_selector("body[data-done='1']")
        page.evaluate("document.fonts.ready")
        page.wait_for_timeout(300)

        check(not errors, f"no console/page errors {errors}")
        check(page.locator(".katex-error").count() == 0, "zero .katex-error")
        html = page.inner_html("#out")
        check("MATHPH" not in html and "CODEPH" not in html, "no leftover placeholders")
        text = page.inner_text("#out")
        check("Costs $5 and $10 and a bonus of $20" in text, "dollar amounts survived as text")
        check("costs $5" in page.locator("table").first.inner_text(), "dollar amount in table cell survived")
        dollar_cache = page.locator("table").nth(1)
        check(dollar_cache.locator(".katex").count() == 3 and "$\\$$" not in dollar_cache.inner_text(),
              "escaped-dollar math ($\\$$ = cache) renders in a table")

        codes = page.locator("pre code").all_inner_texts()
        check(len(codes) == 2, f"2 fenced code blocks ({len(codes)})")
        check(any('echo "$HOME is home"; for f in $FILES' in c for c in codes), "shell code keeps $")
        check(any('printf("$%d and $%s\\n"' in c for c in codes), "printf code keeps $")
        inline = page.locator("p code").all_inner_texts()
        check("$HOME" in inline and 'printf("$%d")' in inline, f"inline code keeps $ {inline}")

        # math counts: inline + display rendering
        check(page.locator(".katex-display").count() >= 4, f">=4 display equations ({page.locator('.katex-display').count()})")
        check(page.locator(".katex").count() >= 18, f"many katex nodes ({page.locator('.katex').count()})")
        cell = page.locator("table").first.locator("tr:nth-child(1) td:nth-child(2) .katex").count()
        check(cell == 1, "|x| rendered as math in table cell")
        check(page.locator("table").first.locator("th").count() == 3, "table has 3 header cells")
        check(page.locator(".callout-warn").count() == 1, "one warning callout")
        check(page.locator(".callout-takeaway").count() == 1, "one takeaway callout")
        check(page.locator(".bg-marker").count() == 1, "background marker")
        check(page.locator("blockquote").count() == 1, "blockquote")
        check(page.locator("ol > li").count() >= 4, "ordered list items")
        check(page.locator("pre code.hljs").count() >= 1, "highlight.js applied")
        # display math scrolls rather than overflowing the page
        sw = page.evaluate("document.documentElement.scrollWidth")
        check(sw <= 900, f"no page horizontal overflow (scrollWidth={sw})")

        SHOT.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(SHOT), full_page=True)
        print("screenshot:", SHOT)
        browser.close()

    print("FAILED" if failures else "ALL OK")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
