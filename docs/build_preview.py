"""Build a standalone preview page with the diagrams inlined.

Chrome refuses to load file:// subresources from a file:// page, so a preview that references
the SVGs by path shows nothing but broken images. Inlining them makes the page a single file
that opens anywhere, with no server and no path resolution involved.

    python docs/build_preview.py && open docs/preview.html
"""

from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
IMG = HERE / "img"

DIAGRAMS = [
    ("pipeline", "1 · Pipeline — เส้นทางสองทาง ทางเดียวที่ใช้ AI"),
    ("benchmark", "2 · Benchmark — 11 โมเดลบนลายมือไทยจริง"),
    ("crop-strategy", "3 · Crop strategy — จับกรอบก่อนอ่านแบบไหนแม่นกว่า"),
    ("impact", "4 · In production — ตัวเลขจาก dashboard จริง"),
]

PAGE = """<!doctype html>
<html lang="th">
<head>
<meta charset="utf-8">
<title>README diagrams — preview</title>
<style>
  :root {{ --bg:#fff; --ink:#1f2430; --muted:#57606a; --line:#d0d7de; --card:#f6f8fa; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Noto Sans Thai",sans-serif;
          background:var(--bg); color:var(--ink); }}
  header {{ position:sticky; top:0; z-index:10; background:var(--bg); border-bottom:1px solid var(--line);
            padding:14px 24px; display:flex; gap:16px; align-items:center; flex-wrap:wrap; }}
  h1 {{ font-size:16px; margin:0; font-weight:700; }}
  .hint {{ font-size:13px; color:var(--muted); }}
  .toggle {{ margin-left:auto; display:flex; gap:6px; }}
  button {{ font:inherit; font-size:13px; padding:7px 14px; border-radius:8px; cursor:pointer;
            border:1px solid var(--line); background:var(--card); color:var(--ink); }}
  button[aria-pressed="true"] {{ background:#2f6f73; border-color:#2f6f73; color:#fff; }}
  main {{ max-width:1100px; margin:0 auto; padding:24px; }}
  section {{ margin-bottom:34px; }}
  h2 {{ font-size:14px; margin:0 0 4px; font-weight:650; }}
  .file {{ font-size:12px; color:var(--muted); margin:0 0 10px; font-family:ui-monospace,monospace; }}
  .frame {{ border:1px solid var(--line); border-radius:12px; overflow:hidden; background:#fff; }}
  .frame.night {{ background:#0d1117; border-color:#30363d; }}
  .frame svg {{ display:block; width:100%; height:auto; }}
  .both {{ display:grid; gap:14px; }}
  @media (min-width:1000px) {{ .both.side {{ grid-template-columns:1fr 1fr; }} }}
  .lbl {{ font-size:11px; color:var(--muted); margin:0 0 5px; }}
</style>
</head>
<body>
<header>
  <h1>README diagrams</h1>
  <span class="hint">ดูก่อน push · บอกได้เลยว่าจะแก้ตรงไหน</span>
  <div class="toggle">
    <button id="b-day" aria-pressed="true">EN สว่าง</button>
    <button id="b-night" aria-pressed="false">EN มืด</button>
    <button id="b-th-day" aria-pressed="false">ไทย สว่าง</button>
    <button id="b-th-night" aria-pressed="false">ไทย มืด</button>
  </div>
</header>
<main id="main"></main>
<script>
const SVG = {svg_json};
const DIAGRAMS = {list_json};
const MODES = ["day", "night", "th-day", "th-night"];
let mode = "day";
function render() {{
  const dark = mode.endsWith("night");
  document.getElementById("main").innerHTML = DIAGRAMS.map(([name, title]) => {{
    return `<section><h2>${{title}}</h2>
            <p class="file">docs/img/${{name}}-${{mode}}.svg</p>
            <div class="frame ${{dark ? "night" : "day"}}">${{SVG[name + "-" + mode]}}</div>
            </section>`;
  }}).join("");
  for (const m of MODES)
    document.getElementById("b-" + m).setAttribute("aria-pressed", String(m === mode));
}}
for (const m of MODES)
  document.getElementById("b-" + m).onclick = () => {{ mode = m; render(); }};
render();
</script>
</body>
</html>
"""


def main() -> None:
    svgs = {}
    for name, _ in DIAGRAMS:
        for theme in ("day", "night", "th-day", "th-night"):
            path = IMG / f"{name}-{theme}.svg"
            if not path.exists():
                raise SystemExit(f"missing {path} — run docs/make_diagrams.py first")
            svgs[f"{name}-{theme}"] = path.read_text(encoding="utf-8")

    out = HERE / "preview.html"
    out.write_text(
        PAGE.format(svg_json=json.dumps(svgs), list_json=json.dumps(DIAGRAMS, ensure_ascii=False)),
        encoding="utf-8",
    )
    print(f"  {out}  ({out.stat().st_size / 1024:.0f} KB, {len(svgs)} diagrams inlined)")


if __name__ == "__main__":
    main()
