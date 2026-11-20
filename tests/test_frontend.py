"""The single-page UI is served by FastAPI and wired to the right endpoints (Task 5).

Interactive behaviour (typing, filters, drawer, keyboard navigation, mobile layout) was
verified manually in a browser - see README "Testing".
"""

import re

from app.api.main import FRONTEND_DIR


def test_frontend_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "EHR Media Intelligence" in r.text
    assert client.head("/").status_code == 200
    assert client.get("/static/app.js").status_code == 200
    css = client.get("/static/styles.css")
    assert css.status_code == 200 and "tailwindcss" in css.text


def test_markup_accessibility_basics():
    html = (FRONTEND_DIR / "index.html").read_text(encoding="utf-8")
    assert 'role="search"' in html and 'role="dialog"' in html and 'aria-modal="true"' in html
    assert 'aria-live="polite"' in html
    # every form control has a <label for=...>
    control_ids = re.findall(r'<(?:input|select)[^>]*\bid="([^"]+)"', html)
    assert {"search-input", "filter-type", "filter-from", "filter-to"} <= set(control_ids)
    for control_id in control_ids:
        assert f'for="{control_id}"' in html, control_id


def test_js_calls_documented_endpoints():
    js = (FRONTEND_DIR / "app.js").read_text(encoding="utf-8")
    for endpoint in ("/search?", "/patients/", "/health", "/bundle", "/resources/"):
        assert endpoint in js
    assert 'method: "POST"' in js and "AbortController" in js


def test_ui_uses_three_colour_palette():
    """Design rule from the README: slate (neutral), brand teal (actions / good states) and red
    (needs attention) are the only hues in the UI."""
    tailwind_hues = {"slate", "gray", "zinc", "neutral", "stone", "red", "orange", "amber", "yellow", "lime",
                     "green", "emerald", "teal", "cyan", "sky", "blue", "indigo", "violet", "purple", "fuchsia",
                     "pink", "rose", "brand"}
    source = "".join((FRONTEND_DIR / f).read_text(encoding="utf-8") for f in ("index.html", "app.js", "src/input.css"))
    used = {h for h in re.findall(r"-([a-z]+)-\d{2,3}\b", source) if h in tailwind_hues}
    assert used == {"slate", "brand", "red"}, used
