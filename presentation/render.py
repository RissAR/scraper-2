#!/usr/bin/env python3
"""
RieltorBot Longread Screenshot Generator
Loads presentation/index.html using Playwright and exports a pixel-perfect PNG.
"""
import sys
import os
from pathlib import Path
from playwright.sync_api import sync_playwright

def render_longread():
    base_dir = Path(__file__).resolve().parent
    html_file = base_dir / "index.html"
    output_png = base_dir / "rieltorbot_longread.png"
    
    if not html_file.exists():
        print(f"Error: {html_file} does not exist", file=sys.stderr)
        sys.exit(1)

    file_url = f"file://{html_file.absolute()}"
    print(f"[*] Loading template: {file_url}")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-web-security"]
        )
        # 1600px width, device_scale_factor=1 (or 2 for Retina quality)
        context = browser.new_context(
            viewport={"width": 1600, "height": 3900},
            device_scale_factor=1
        )
        page = context.new_page()
        page.goto(file_url, wait_until="networkidle")

        # Give a small pause for Google Fonts / Tailwind CDN to complete paint
        page.wait_for_timeout(1500)

        # Get exact dimensions of document body
        body_handle = page.query_selector("body")
        box = body_handle.bounding_box() if body_handle else None
        
        print(f"[*] Document height measured: {box['height'] if box else 'unknown'}px")

        print(f"[*] Capturing screenshot to {output_png}...")
        page.screenshot(
            path=str(output_png),
            full_page=True,
            type="png"
        )
        
        browser.close()

    print(f"[✔] Successfully generated: {output_png}")
    print(f"    File size: {output_png.stat().st_size / 1024:.1f} KB")

if __name__ == "__main__":
    render_longread()
