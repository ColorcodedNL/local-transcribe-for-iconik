"""Maakt de PDF-handleidingen uit docs/guide/*.html.

Draaien:  uv run --no-project --with playwright python -B scripts/build_guides.py
Eenmalig: uv run --no-project --with playwright playwright install chromium
Met LT_CHROMIUM=/pad/naar/chrome gebruikt hij die browser in plaats van die van Playwright.
"""
import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
GUIDE = ROOT / "docs" / "guide"
OUT = {
    "en.html": ROOT / "docs" / "Guide Local Transcribe.pdf",
    "nl.html": ROOT / "docs" / "Handleiding Local Transcribe.pdf",
}
FOOTER = ('<div style="width:100%;font:8pt -apple-system,Helvetica,Arial,sans-serif;color:#6e6e73;'
          'text-align:center"><span class="pageNumber"></span> / <span class="totalPages"></span></div>')


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=os.environ.get("LT_CHROMIUM") or None)
        page = browser.new_page()
        for name, pdf in OUT.items():
            page.goto((GUIDE / name).as_uri())
            page.wait_for_load_state("networkidle")
            missing = page.evaluate("[...document.images].filter(i => !i.naturalWidth).map(i => i.getAttribute('src'))")
            if missing:
                sys.exit(f"{name}: afbeelding ontbreekt: {', '.join(missing)}")
            page.pdf(path=str(pdf), prefer_css_page_size=True, print_background=True,
                     display_header_footer=True, header_template="<div></div>", footer_template=FOOTER)
            print(f"{pdf.relative_to(ROOT)}  {pdf.stat().st_size // 1024} KB")
        browser.close()


if __name__ == "__main__":
    main()
