"""Render WRITEUP.md to a one-page PDF (docs/WRITEUP.pdf) using headless Microsoft Edge / Chrome.

    python scripts/build_writeup_pdf.py

Only the small Markdown subset used in WRITEUP.md is supported (headings, bullets,
numbered lists with nested bullets, **bold**, `code`), which avoids a markdown dependency.
"""

from __future__ import annotations

import html
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "WRITEUP.md"
OUT_HTML = ROOT / "docs" / "writeup.html"
OUT_PDF = ROOT / "docs" / "WRITEUP.pdf"

CSS = """
@page { size: Letter; margin: 0.45in 0.55in; }
body { font-family: 'Segoe UI', Arial, sans-serif; font-size: 8.6pt; line-height: 1.32; color: #1e293b; }
h1 { font-size: 13.5pt; margin: 0 0 4pt; color: #133f3a; border-bottom: 2px solid #2a9d8f; padding-bottom: 3pt; }
h2 { font-size: 9.8pt; margin: 7pt 0 2pt; color: #1b6a60; }
ul, ol { margin: 0 0 0 13pt; padding: 0; }
ul ul { margin-top: 1pt; }
li { margin: 0 0 1.6pt; }
p { margin: 0 0 3pt; }
code { font-family: Consolas, monospace; font-size: 8pt; background: #f1f5f9; padding: 0 2px; border-radius: 2px; }
strong { color: #0f172a; }
.meta { color: #64748b; font-size: 8pt; margin-bottom: 4pt; }
"""


def inline(text: str) -> str:
    text = html.escape(text, quote=False)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    return re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)


def convert(md: str) -> str:
    out: list[str] = []
    stack: list[str] = []  # open list tags, by depth

    def close_to(depth: int) -> None:
        while len(stack) > depth:
            out.append(f"</li></{stack.pop()}>")

    for raw in md.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        if line.startswith("# "):
            close_to(0)
            out.append(f"<h1>{inline(line[2:])}</h1>")
            out.append('<p class="meta">Onye 2026 AI Full-stack Assessment &middot; Python / FastAPI / FHIR R4 / '
                       "Claude / FAISS / Tailwind</p>")
            continue
        if line.startswith("## "):
            close_to(0)
            out.append(f"<h2>{inline(line[3:])}</h2>")
            continue
        m = re.match(r"^(\s*)([-*]|\d+\.)\s+(.*)$", line)
        if m:
            depth = len(m.group(1)) // 2 + 1 if m.group(1) else 1
            tag = "ol" if m.group(2)[0].isdigit() else "ul"
            if len(stack) >= depth:
                close_to(depth)
                out.append(f"</li><li>{inline(m.group(3))}")
            else:
                while len(stack) < depth:
                    out.append(f"<{tag}><li>")
                    stack.append(tag)
                out[-1] += inline(m.group(3))
            continue
        # continuation paragraph inside a list item
        if stack and raw.startswith(" "):
            out.append(f"<br>{inline(line.strip())}")
            continue
        close_to(0)
        out.append(f"<p>{inline(line)}</p>")
    close_to(0)
    return "\n".join(out)


def find_browser() -> str:
    candidates = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    ]
    for c in candidates:
        if Path(c).exists():
            return c
    for name in ("msedge", "google-chrome", "chromium", "chrome"):
        if found := shutil.which(name):
            return found
    sys.exit("No Edge/Chrome found to print the PDF")


def main() -> None:
    body = convert(SRC.read_text(encoding="utf-8"))
    OUT_HTML.parent.mkdir(exist_ok=True)
    OUT_HTML.write_text(f"<!doctype html><html lang='en'><head><meta charset='utf-8'><title>Write-up</title>"
                        f"<style>{CSS}</style></head><body>{body}</body></html>", encoding="utf-8")
    subprocess.run([find_browser(), "--headless", "--disable-gpu", "--no-pdf-header-footer",
                    f"--print-to-pdf={OUT_PDF}", OUT_HTML.as_uri()], check=True, capture_output=True, timeout=120)
    print(f"Wrote {OUT_PDF}")


if __name__ == "__main__":
    main()
