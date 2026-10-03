"""Generate the README diagrams as SVG, in a light and a dark variant.

Hand-drawn diagrams rot: the numbers in them drift away from the numbers they came from. So
these are generated, and the figures below are the same ones that appear in bench/report.md,
bench/crop_report.md and the production dashboard.

    python docs/make_diagrams.py

Writes docs/img/<name>-day.svg and <name>-night.svg. The README pairs them with <picture> and
prefers-color-scheme, which is how GitHub serves the right one to each reader.

SVG referenced from an <img> is sandboxed: no external fonts load, so everything here uses the
system stack, and no diagram depends on a web font being available.
"""

from __future__ import annotations

import html
from pathlib import Path

OUT = Path(__file__).resolve().parent / "img"

FONT = ("-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, "
        "'Noto Sans Thai', sans-serif")

# Palettes. The accents keep their identity across themes; only lightness moves, so the two
# variants read as the same diagram rather than two different ones.
DAY = {
    "bg": "#ffffff", "surface": "#f6f8fa", "surface2": "#eef1f5",
    "ink": "#1f2430", "muted": "#57606a", "line": "#d0d7de",
    "teal": "#2f6f73", "teal_soft": "#dcebec",
    "orange": "#e8743b", "orange_soft": "#fde8dd",
    "green": "#1a7f47", "green_soft": "#ddf4e6",
    "grey": "#8b949e",
}
NIGHT = {
    "bg": "#0d1117", "surface": "#161b22", "surface2": "#1c2128",
    "ink": "#e6edf3", "muted": "#9198a1", "line": "#30363d",
    "teal": "#56b6b3", "teal_soft": "#16302f",
    "orange": "#f0883e", "orange_soft": "#3a2217",
    "green": "#3fb950", "green_soft": "#12261a",
    "grey": "#6e7681",
}


# Every drawn element records the box it occupies, so main() can assert that nothing spills
# outside the canvas. Diagrams are generated, which means a layout bug is silent — it produces a
# valid SVG with text hanging off the edge. This is the check that makes it loud.
_INK: list[tuple[float, float, float, float, str]] = []


def _reset_ink() -> None:
    _INK.clear()


def _ink(x1, y1, x2, y2, what) -> None:
    _INK.append((x1, y1, x2, y2, what))


# Rough advance width per character. Latin at ~0.52em is close enough for a bounds check; Thai
# needs more because of the taller clusters, so it is measured separately and the wider of the
# two wins.
def text_width(s: str, size: float) -> float:
    latin = sum(1 for c in s if ord(c) < 0x0E00)
    thai = len(s) - latin
    return size * (0.54 * latin + 0.62 * thai)


def esc(s: str) -> str:
    return html.escape(str(s), quote=True)


def text(x, y, s, *, size=13, fill="ink", weight=400, anchor="start", p=None, opacity=None):
    w = text_width(str(s), size)
    x1 = x if anchor == "start" else (x - w if anchor == "end" else x - w / 2)
    _ink(x1, y - size * 0.82, x1 + w, y + size * 0.26, f"text {s!r:.40}")
    o = f' opacity="{opacity}"' if opacity is not None else ""
    return (f'<text x="{x}" y="{y}" font-family="{FONT}" font-size="{size}" '
            f'font-weight="{weight}" fill="{p[fill] if fill in p else fill}" '
            f'text-anchor="{anchor}"{o}>{esc(s)}</text>')


def box(x, y, w, h, *, fill="surface", stroke="line", r=10, p=None, sw=1, dash=None):
    _ink(x, y, x + w, y + h, "box")
    d = f' stroke-dasharray="{dash}"' if dash else ""
    return (f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{r}" '
            f'fill="{p[fill] if fill in p else fill}" '
            f'stroke="{p[stroke] if stroke in p else stroke}" stroke-width="{sw}"{d}/>')


def arrow(x1, y1, x2, y2, *, p, color="line", head=6):
    """A straight connector with a solid arrowhead at the far end."""
    c = p[color] if color in p else color
    if y1 == y2:
        tip = f'{x2},{y2} {x2 - head},{y2 - head * 0.6} {x2 - head},{y2 + head * 0.6}'
        line = f'<line x1="{x1}" y1="{y1}" x2="{x2 - head}" y2="{y2}" stroke="{c}" stroke-width="1.6"/>'
    else:
        tip = f'{x2},{y2} {x2 - head * 0.6},{y2 - head} {x2 + head * 0.6},{y2 - head}'
        line = f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2 - head}" stroke="{c}" stroke-width="1.6"/>'
    return line + f'<polygon points="{tip}" fill="{c}"/>'


def svg(w, h, body, p):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
            f'viewBox="0 0 {w} {h}" role="img">\n'
            f'<rect width="{w}" height="{h}" fill="{p["bg"]}"/>\n{body}\n</svg>\n')


# Label tables. The Thai README is the fuller document and its readers should not have to
# translate a diagram in their head, so every diagram is emitted in both languages. Technical
# terms stay in English in the Thai table, matching how the Thai README already writes them.
EN = {
    "pipe_title": "How a car gets back to its owner",
    "pipe_sub": "Two ways in. Only one of them needs the AI.",
    "lane_a": "A · HAND-WRITTEN SLIP",
    "lane_b": "B · DRIVER FILLS IT IN  ·  NO AI, NO QUEUE",
    "n_photo": ("Photograph", ["slip on a phone", "HEIC, any angle"]),
    "n_crop": ("Crop the paper", ["OpenCV, scored", "falls back to full frame"]),
    "n_llm": ("LLM reads it", ["Gemini 3 Flash", "confidence per field"]),
    "n_human": ("Human confirms", ["review queue", "claimed, never shared"]),
    "n_typed": ("Driver types it", ["on their own phone", "dropdowns, not free text"]),
    "n_pass": ("Staff passcode", ['"I saw this car"', "server-side only"]),
    "on_spot": "approved on the spot",
    "store": "Postgres",
    "store_sub": "+ the photo as evidence",
    "search": "Fuzzy search",
    "search_1": "trigram + levenshtein,",
    "search_2": "reranked in Python",
    "pipe_foot": ("The review queue only ever sees lane A. Lane B was the last feature shipped, "
                  "and it is what removed the bottleneck."),

    "bench_title": "11 vision models on real Thai handwriting",
    "bench_sub": ("Each model's best preprocessing variant. Accuracy is the mean across the core "
                  "fields, scored after normalization."),
    "h_acc": "MEAN CORE ACCURACY",
    "h_cost": "COST / 1000 SLIPS",
    "bench_foot": ("Winner on accuracy and among the cheapest. The most expensive model scored "
                   "worst — 27x the cost for 20 points less."),
    "bench_src": "Full table: bench/report.md",

    "crop_title": "Does it matter how you find the paper first?",
    "crop_sub": ("Same model throughout, 49 human-labelled slips. Only the image sent differs. "
                 "Lower character error rate is better."),
    "crop_foot": ("Cropping halves the error on photos taken from a distance (0.260 -> 0.132). "
                  "Having the LLM find the corners is worse than not cropping at all."),
    "crop_src": "Full table: bench/crop_report.md",
    "s_cv": ("OpenCV crop", "best, cheapest, fastest"),
    "s_deskew": ("OpenCV crop + deskew", "rotation blurs small text"),
    "s_full": ("Full frame, no crop", "more tokens, worse reads"),
    "s_llm": ("LLM finds the quad", "46% dearer, 2x slower"),

    "imp_title": "In production",
    "imp_sub": "Figures from the live dashboard.",
    "k_slips": "slips in the system",
    "k_peak": "busiest single day",
    "k_back": "cars handed back",
    "k_spend": "total AI spend",
    "k_note": "about THB 335",
    "imp_foot": ("The land saved the cars from the water. This saved the ability to give them "
                 "back — and ran on $10 of inference."),
}

TH = {
    "pipe_title": "รถกลับไปหาเจ้าของได้อย่างไร",
    "pipe_sub": "ทางเข้ามีสองทาง มีทางเดียวที่ต้องใช้ AI",
    "lane_a": "A · ใบที่เขียนด้วยลายมือ",
    "lane_b": "B · ผู้มาจอดกรอกเอง  ·  ไม่ใช้ AI ไม่เข้าคิว",
    "n_photo": ("ถ่ายรูปใบ", ["ถ่ายด้วยมือถือ", "HEIC มุมไหนก็ได้"]),
    "n_crop": ("จับกรอบกระดาษ", ["OpenCV ให้คะแนนเลือก", "ถ้าไม่เจอใช้ภาพเต็ม"]),
    "n_llm": ("LLM อ่านใบ", ["Gemini 3 Flash", "บอกความมั่นใจรายช่อง"]),
    "n_human": ("คนตรวจยืนยัน", ["คิวตรวจสอบ", "จองใบ ไม่ซ้ำกัน"]),
    "n_typed": ("ผู้มาจอดพิมพ์เอง", ["บนมือถือตัวเอง", "เลือกจากรายการ ไม่พิมพ์อิสระ"]),
    "n_pass": ("รหัสเจ้าหน้าที่", ['"เห็นรถคันนี้จอดจริง"', "ตรวจฝั่ง server เท่านั้น"]),
    "on_spot": "อนุมัติทันที",
    "store": "Postgres",
    "store_sub": "+ รูปหลักฐานของใบ",
    "search": "ค้นแบบ fuzzy",
    "search_1": "trigram + levenshtein",
    "search_2": "แล้วจัดอันดับใน Python",
    "pipe_foot": ("คิวตรวจเห็นเฉพาะแถว A เท่านั้น ส่วนแถว B คือฟีเจอร์สุดท้ายที่ทำ "
                  "และเป็นตัวที่ทำให้คอขวดหายไป"),

    "bench_title": "วัด 11 vision model บนลายมือไทยจริง",
    "bench_sub": ("เอา preprocessing ที่ดีที่สุดของแต่ละ model มาเทียบ ความแม่นคือค่าเฉลี่ยของช่องหลัก "
                  "วัดหลัง normalize แล้ว"),
    "h_acc": "ความแม่นเฉลี่ยช่องหลัก",
    "h_cost": "ราคาต่อ 1000 ใบ",
    "bench_foot": ("แม่นที่สุดและถูกเกือบที่สุด ส่วนตัวที่แพงที่สุดกลับแม่นน้อยที่สุด "
                   "— แพงกว่า 27 เท่าแต่ได้น้อยกว่า 20 จุด"),
    "bench_src": "ตารางเต็ม: bench/report.md",

    "crop_title": "จับกรอบกระดาษก่อนอ่าน มีผลไหม",
    "crop_sub": ("ใช้ model เดียวกันหมด ใบที่คนยืนยันแล้ว 49 ใบ ต่างกันแค่ภาพที่ส่งเข้าไป "
                 "ค่า CER ยิ่งต่ำยิ่งดี"),
    "crop_foot": ("การ crop ลดความผิดพลาดของใบที่ถ่ายไกลลงครึ่งหนึ่ง (0.260 -> 0.132) "
                  "ส่วนการให้ LLM หามุมกระดาษ แย่กว่าการไม่ crop เลย"),
    "crop_src": "ตารางเต็ม: bench/crop_report.md",
    "s_cv": ("OpenCV crop", "แม่นสุด ถูกสุด เร็วสุด"),
    "s_deskew": ("OpenCV crop + แก้เอียง", "การหมุนทำให้ตัวเล็กเบลอ"),
    "s_full": ("ภาพเต็ม ไม่ crop", "เปลือง token และอ่านแย่กว่า"),
    "s_llm": ("ให้ LLM หามุมกระดาษ", "แพงกว่า 46% ช้ากว่าเท่าตัว"),

    "imp_title": "ของจริงบน production",
    "imp_sub": "ตัวเลขจาก dashboard ที่ใช้งานอยู่",
    "k_slips": "ใบในระบบ",
    "k_peak": "วันที่เข้ามากที่สุด",
    "k_back": "คันที่รับรถกลับแล้ว",
    "k_spend": "ค่า AI รวมทั้งหมด",
    "k_note": "ประมาณ 335 บาท",
    "imp_foot": ("ที่ดินของวัดช่วยรถให้พ้นน้ำ ส่วนระบบนี้ช่วยให้คืนรถถูกคันได้ "
                 "— และใช้ค่า inference ไปทั้งหมด $10"),
}


# ---------------------------------------------------------------- 1. the pipeline

def pipeline(p, t) -> tuple[int, int, str]:
    W, H = 980, 430
    o: list[str] = []
    o.append(text(28, 38, t["pipe_title"], size=19, weight=700, p=p))
    o.append(text(28, 60, t["pipe_sub"], size=13, fill="muted", p=p))

    def node(x, y, w, h, title, sub, *, accent="teal", soft="teal_soft"):
        parts = [box(x, y, w, h, fill=soft, stroke=accent, p=p, r=12)]
        parts.append(text(x + w / 2, y + 30, title, size=13.5, weight=650, anchor="middle", p=p))
        for i, line in enumerate(sub):
            parts.append(text(x + w / 2, y + 50 + i * 15, line, size=11,
                              fill="muted", anchor="middle", p=p))
        return "".join(parts)

    # Lane A — the paper path
    ay = 104
    o.append(text(28, ay - 12, t["lane_a"], size=10.5, weight=700, fill="orange", p=p))
    xs = [28, 212, 396, 580]
    w, h = 160, 84
    o.append(node(xs[0], ay, w, h, *t["n_photo"],
                  accent="orange", soft="orange_soft"))
    o.append(node(xs[1], ay, w, h, *t["n_crop"],
                  accent="orange", soft="orange_soft"))
    o.append(node(xs[2], ay, w, h, *t["n_llm"],
                  accent="orange", soft="orange_soft"))
    o.append(node(xs[3], ay, w, h, *t["n_human"],
                  accent="orange", soft="orange_soft"))
    for i in range(3):
        o.append(arrow(xs[i] + w, ay + h / 2, xs[i + 1], ay + h / 2, p=p, color="orange"))

    # Lane B — self-service
    by = 254
    o.append(text(28, by - 12, t["lane_b"], size=10.5, weight=700, fill="green", p=p))
    o.append(node(xs[0], by, w, h, *t["n_typed"],
                  accent="green", soft="green_soft"))
    o.append(node(xs[1], by, w, h, *t["n_pass"],
                  accent="green", soft="green_soft"))
    o.append(arrow(xs[0] + w, by + h / 2, xs[1], by + h / 2, p=p, color="green"))
    o.append(f'<line x1="{xs[1] + w}" y1="{by + h / 2}" x2="{xs[3] + w / 2}" y2="{by + h / 2}" '
             f'stroke="{p["green"]}" stroke-width="1.6"/>')
    o.append(text((xs[1] + w + xs[3]) / 2 + 20, by + h / 2 - 9, t["on_spot"],
                  size=10.5, fill="green", anchor="middle", p=p))

    # Converge into the store
    sx, sy, sw_, sh_ = 764, 148, 188, 148
    o.append(box(sx, sy, sw_, sh_, fill="surface", stroke="teal", p=p, r=12, sw=1.6))
    o.append(text(sx + sw_ / 2, sy + 32, t["store"], size=14, weight=700, anchor="middle", p=p))
    o.append(text(sx + sw_ / 2, sy + 52, t["store_sub"], size=11, fill="muted",
                  anchor="middle", p=p))
    o.append(f'<line x1="{sx + 22}" y1="{sy + 68}" x2="{sx + sw_ - 22}" y2="{sy + 68}" '
             f'stroke="{p["line"]}" stroke-width="1"/>')
    o.append(text(sx + sw_ / 2, sy + 90, t["search"], size=13, weight=650,
                  anchor="middle", fill="teal", p=p))
    o.append(text(sx + sw_ / 2, sy + 109, t["search_1"], size=10.5, fill="muted",
                  anchor="middle", p=p))
    o.append(text(sx + sw_ / 2, sy + 124, t["search_2"], size=10.5, fill="muted",
                  anchor="middle", p=p))
    o.append(arrow(xs[3] + w, ay + h / 2, sx, sy + 46, p=p, color="orange"))
    o.append(arrow(xs[3] + w / 2, by + h / 2, sx, sy + 110, p=p, color="green"))

    o.append(text(28, H - 22, t["pipe_foot"], size=11.5, fill="muted", p=p))
    return W, H, "".join(o)


# ---------------------------------------------------------------- 2. model benchmark

MODELS = [
    ("google/gemini-3-flash-preview", "v1_crop", 78, 1.55, 2.7, True),
    ("google/gemini-3.1-pro-preview", "v1_crop", 77, 7.05, 4.7, False),
    ("google/gemini-3.6-flash", "v1_crop", 75, 2.59, 4.3, False),
    ("google/gemini-3.5-flash", "v1_crop", 75, 6.52, 3.7, False),
    ("google/gemini-3.1-flash-lite", "v0_raw", 73, 1.02, 8.4, False),
    ("google/gemini-3.8-flash", "v1_crop", 73, 2.23, 4.9, False),
    ("google/gemini-3.7-flash", "v2_enhanced", 73, 2.45, 4.7, False),
    ("google/gemini-2.5-flash", "v1_crop", 72, 3.02, 7.4, False),
    ("qwen/qwen3-vl-235b-a22b-instruct", "v1_crop", 70, 0.69, 10.5, False),
    ("google/gemini-2.5-pro", "v0_raw", 70, 11.55, 12.4, False),
    ("anthropic/claude-opus-5.5", "v0_raw", 58, 41.86, 30.8, False),
]


def benchmark(p, t) -> tuple[int, int, str]:
    W = 980
    row, top = 30, 116
    H = top + row * len(MODELS) + 74
    o: list[str] = []
    o.append(text(28, 38, t["bench_title"], size=19, weight=700, p=p))
    o.append(text(28, 60, t["bench_sub"], size=12.5, fill="muted", p=p))

    x0, bar_max = 332, 384
    o.append(text(x0, top - 18, t["h_acc"], size=9.5, weight=700, fill="muted", p=p))
    o.append(text(x0 + bar_max + 118, top - 18, t["h_cost"], size=9.5, weight=700,
                  fill="muted", anchor="middle", p=p))
    o.append(text(W - 36, top - 18, "p50", size=9.5, weight=700, fill="muted", anchor="end", p=p))

    for i, (name, variant, acc, cost, p50, win) in enumerate(MODELS):
        y = top + i * row
        cy = y + row / 2
        if win:
            o.append(box(20, y + 1, W - 40, row - 2, fill="teal_soft", stroke="teal_soft",
                         p=p, r=7))
        o.append(text(28, cy + 4, name, size=12, weight=700 if win else 400,
                      fill="ink" if win else "muted", p=p))
        o.append(text(300, cy + 4, variant, size=10, fill="grey", anchor="end", p=p))

        bw = bar_max * (acc / 100)
        o.append(f'<rect x="{x0}" y="{cy - 7}" width="{bar_max}" height="14" rx="7" '
                 f'fill="{p["surface2"]}"/>')
        o.append(f'<rect x="{x0}" y="{cy - 7}" width="{bw:.1f}" height="14" rx="7" '
                 f'fill="{p["teal"] if win else p["grey"]}"/>')
        o.append(text(x0 + bw + 10, cy + 4, f"{acc}%", size=12,
                      weight=700 if win else 500, fill="teal" if win else "muted", p=p))

        # cost chip — width carries the magnitude so $41.86 reads as the outlier it is
        chip_x, chip_max = x0 + bar_max + 56, 124
        cw = max(26, chip_max * (cost / 41.86) ** 0.5)
        o.append(f'<rect x="{chip_x}" y="{cy - 9}" width="{cw:.1f}" height="18" rx="9" '
                 f'fill="{p["orange_soft"] if cost > 10 else p["surface2"]}"/>')
        o.append(text(chip_x + 10, cy + 4, f"${cost:.2f}", size=11.5,
                      fill="orange" if cost > 10 else "muted", weight=600 if cost > 10 else 400, p=p))
        o.append(text(W - 36, cy + 4, f"{p50:.1f}s", size=11, fill="grey", anchor="end", p=p))

    o.append(text(28, H - 38, t["bench_foot"], size=12, fill="muted", p=p))
    o.append(text(28, H - 18, t["bench_src"], size=11, fill="grey", p=p))
    return W, H, "".join(o)


# ---------------------------------------------------------------- 3. crop strategy

# Numbers live here; the label and the aside come from the language table, keyed by the first
# element, so a translation can never drift from the figure it belongs to.
STRATEGIES = [
    ("s_cv", 0.124, "$1.86", True),
    ("s_deskew", 0.131, "$1.84", False),
    ("s_full", 0.161, "$2.16", False),
    ("s_llm", 0.184, "$2.72", False),
]


def crop(p, t) -> tuple[int, int, str]:
    W = 980
    top, row = 98, 44
    # Height follows the content instead of being guessed: bars, then a fixed footer. Guessing it
    # is what put the caption on top of the last bar.
    H = top + len(STRATEGIES) * row + 76
    o: list[str] = []
    o.append(text(28, 38, t["crop_title"], size=19, weight=700, p=p))
    o.append(text(28, 60, t["crop_sub"], size=12.5, fill="muted", p=p))

    # bar_max is sized so the notes column still clears the right margin by a comfortable gap
    x0, bar_max = 236, 400
    for i, (key, cer, cost, win) in enumerate(STRATEGIES):
        name, note = t[key]
        cy = top + i * row + 16
        o.append(text(220, cy + 4, name, size=12.5, weight=700 if win else 400,
                      fill="ink" if win else "muted", anchor="end", p=p))
        bw = bar_max * (cer / 0.20)
        o.append(f'<rect x="{x0}" y="{cy - 10}" width="{bar_max}" height="20" rx="10" '
                 f'fill="{p["surface2"]}"/>')
        o.append(f'<rect x="{x0}" y="{cy - 10}" width="{bw:.1f}" height="20" rx="10" '
                 f'fill="{p["teal"] if win else p["grey"]}"/>')
        o.append(text(x0 + bw + 12, cy + 4, f"{cer:.3f}", size=12.5, weight=700 if win else 500,
                      fill="teal" if win else "muted", p=p))
        o.append(text(x0 + bar_max + 76, cy + 4, cost, size=11.5, fill="grey", p=p))
        o.append(text(x0 + bar_max + 140, cy + 4, note, size=11,
                      fill="teal" if win else "grey", p=p))

    o.append(text(28, H - 44, t["crop_foot"], size=12, fill="muted", p=p))
    o.append(text(28, H - 22, t["crop_src"], size=11, fill="grey", p=p))
    return W, H, "".join(o)


# ---------------------------------------------------------------- 4. production impact

STATS = [
    ("8,401", "k_slips", "teal", None),
    ("3,290", "k_peak", "teal", None),
    ("1,110", "k_back", "teal", None),
    ("$10", "k_spend", "orange", "k_note"),
]


def impact(p, t) -> tuple[int, int, str]:
    W, H = 980, 278
    o: list[str] = []
    o.append(text(28, 38, t["imp_title"], size=19, weight=700, p=p))
    o.append(text(28, 60, t["imp_sub"], size=12.5, fill="muted", p=p))

    # Derived, not hand-picked: four cards at 224 came to 984 on a 980 canvas.
    pad, gap, y = 28, 20, 86
    cw = (W - 2 * pad - gap * (len(STATS) - 1)) / len(STATS)
    for i, (num, label_key, colour, note_key) in enumerate(STATS):
        label, note = t[label_key], (t[note_key] if note_key else "")
        x = pad + i * (cw + gap)
        o.append(box(x, y, cw, 104, fill="surface", stroke="line", p=p, r=12))
        o.append(text(x + cw / 2, y + 50, num, size=34, weight=700, fill=colour,
                      anchor="middle", p=p))
        o.append(text(x + cw / 2, y + 74, label, size=12, fill="muted", anchor="middle", p=p))
        if note:
            o.append(text(x + cw / 2, y + 90, note, size=10.5, fill="grey", anchor="middle", p=p))

    o.append(box(28, 214, W - 56, 44, fill="surface2", stroke="surface2", p=p, r=10))
    o.append(text(48, 241, t["imp_foot"], size=12.5, fill="muted", p=p))
    return W, H, "".join(o)


DIAGRAMS = {
    "pipeline": pipeline,
    "benchmark": benchmark,
    "crop-strategy": crop,
    "impact": impact,
}


MARGIN = 14          # how close to the canvas edge anything is allowed to get
OVERLAP_TOL = 2      # rows may share a pixel or two of rounding without it meaning anything


def check(name: str, w: int, h: int) -> list[str]:
    """Report anything spilling off the canvas, or a caption sitting on top of a bar."""
    problems = []
    for x1, y1, x2, y2, what in _INK:
        if x1 < 0 or y1 < 0 or x2 > w or y2 > h:
            problems.append(f"{name}: {what} at ({x1:.0f},{y1:.0f})-({x2:.0f},{y2:.0f}) "
                            f"outside {w}x{h}")
        elif x2 > w - MARGIN:
            problems.append(f"{name}: {what} ends at x={x2:.0f}, under {MARGIN}px from the edge")

    # The failure that actually shipped: a footer line landing on top of the last bar. Two pieces
    # of text can never legitimately occupy the same space, so overlap between them is always a
    # bug — unlike text inside a box, which is the normal case.
    texts = [i for i in _INK if i[4].startswith("text")]
    for i, (ax1, ay1, ax2, ay2, aw) in enumerate(texts):
        for bx1, by1, bx2, by2, bw in texts[i + 1:]:
            if (ax1 < bx2 - OVERLAP_TOL and ax2 > bx1 + OVERLAP_TOL
                    and ay1 < by2 - OVERLAP_TOL and ay2 > by1 + OVERLAP_TOL):
                problems.append(f"{name}: {aw} overlaps {bw}")
    return problems


LANGS = {"": EN, "-th": TH}


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    failures: list[str] = []
    for name, fn in DIAGRAMS.items():
        for suffix, strings in LANGS.items():
            for theme, palette in (("day", DAY), ("night", NIGHT)):
                _reset_ink()
                w, h, body = fn(palette, strings)
                stem = f"{name}{suffix}-{theme}"
                problems = check(stem, w, h)
                failures += problems
                path = OUT / f"{stem}.svg"
                path.write_text(svg(w, h, body, palette), encoding="utf-8")
                flag = "  <-- " + str(len(problems)) + " issue(s)" if problems else ""
                print(f"  {path.relative_to(OUT.parent.parent)}  {w}x{h}{flag}")
    if failures:
        print("\nLayout problems:")
        for f in failures:
            print(f"  {f}")
        raise SystemExit(1)
    print("\n  bounds check passed")


if __name__ == "__main__":
    main()
