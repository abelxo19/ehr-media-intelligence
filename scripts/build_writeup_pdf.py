"""Render WRITEUP.md to a one-page PDF (docs/WRITEUP.pdf) using headless Microsoft Edge / Chrome.

    python scripts/build_writeup_pdf.py

Only the small Markdown subset used in WRITEUP.md is supported (headings, bullets,
**bold**, `code`, a byline and "claim -> evidence" pointers), which avoids a markdown dependency.
"""

from __future__ import annotations

import html
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "WRITEUP.md"
OUT_PDF = ROOT / "docs" / "WRITEUP.pdf"

CSS = """
@page { size: Letter; margin: 0.55in 0.85in 0.5in; }
body { font-family: 'Segoe UI', Arial, sans-serif; font-size: 9.5pt; line-height: 1.42; color: #334155; }
h1 { font-size: 16pt; font-weight: 600; margin: 0; color: #0f172a; letter-spacing: -0.01em; }
.meta { color: #94a3b8; font-size: 8.8pt; margin: 2pt 0 14pt; }
h2 { font-size: 8.4pt; font-weight: 700; text-transform: uppercase; letter-spacing: 0.08em;
     color: #2a9d8f; margin: 12pt 0 4pt; }
ul, ol { margin: 0; padding-left: 12pt; }
li { margin: 0 0 3pt; padding-left: 2pt; }
li::marker { color: #cbd5e1; }
p { margin: 0 0 4pt; }
strong { color: #0f172a; font-weight: 600; }
code { font-family: Consolas, monospace; font-size: 8.6pt; color: #475569; }
.ev { display: block; color: #94a3b8; font-size: 8pt; }
.ev code { color: #94a3b8; font-size: 7.8pt; }
"""
EVIDENCE = " → "  # "claim -> evidence" pointers are rendered small and grey


def _fmt(text: str) -> str:
    text = html.escape(text, quote=False)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    return re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)


def inline(text: str) -> str:
    if EVIDENCE in text:
        claim, evidence = text.split(EVIDENCE, 1)
        return f'{_fmt(claim)}<span class="ev">→ {_fmt(evidence)}</span>'
    return _fmt(text)


def convert(md: str) -> str:
    out: list[str] = []
    stack: list[str] = []  # open list tags, by depth
    after_title = False

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
            after_title = True
            continue
        if after_title:  # the first plain paragraph after the title is the byline
            after_title = False
            if not line.startswith(("#", "-", " ")):
                out.append(f'<p class="meta">{inline(line)}</p>')
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
    with tempfile.TemporaryDirectory() as tmp:
        page = Path(tmp) / "writeup.html"
        page.write_text(f"<!doctype html><html lang='en'><head><meta charset='utf-8'><title>Write-up</title>"
                        f"<style>{CSS}</style></head><body>{body}</body></html>", encoding="utf-8")
        subprocess.run([find_browser(), "--headless", "--disable-gpu", "--no-pdf-header-footer",
                        f"--print-to-pdf={OUT_PDF}", page.as_uri()], check=True, capture_output=True, timeout=120)
    print(f"Wrote {OUT_PDF}")


if __name__ == "__main__":
    main()
