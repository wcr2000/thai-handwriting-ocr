"""ให้คะแนนผลจาก run_bench.py เทียบกับ example/label.json แล้วออกรายงาน bench/report.md

ทุก field ถูก normalize ก่อนเทียบเสมอ (เลขไทย, คำนำหน้าชื่อ, ยี่ห้อภาษาไทย/อังกฤษ, รูปแบบวันที่)
จึงวัด "อ่านถูกไหม" ไม่ใช่ "พิมพ์เหมือนเป๊ะไหม"
"""

from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

from rapidfuzz.distance import Levenshtein

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bench.run_bench import MODELS, VARIANTS, slug
from ocrslip.normalize import normalize_field
from ocrslip.schema import canonicalize

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "bench" / "out"

SCORED_FIELDS = ["name", "tel", "date", "noplate", "brand", "typecar", "location", "province"]
# field ที่ต้องถูกครบถึงจะนับว่า "ใบนี้ถูกทั้งใบ" — location/province ไม่นับเพราะหลายใบไม่ได้เขียน
CORE_FIELDS = ["name", "tel", "date", "noplate", "brand", "typecar"]


def cer(truth: str, got: str) -> float:
    """character error rate: 0 = ตรงเป๊ะ, 1 = ผิดหมด"""
    if not truth:
        return 0.0 if not got else 1.0
    return min(1.0, Levenshtein.distance(truth, got) / len(truth))


def score_run(labels: dict[str, dict], model: str, variant: str) -> dict | None:
    folder = OUT / slug(model) / variant
    if not folder.exists():
        return None

    per_field: dict[str, list[bool]] = {f: [] for f in SCORED_FIELDS}
    near: dict[str, list[bool]] = {f: [] for f in SCORED_FIELDS}
    cers: dict[str, list[float]] = {f: [] for f in SCORED_FIELDS}
    records, errors, latencies = 0, 0, []
    prompt_tokens, completion_tokens, reported_cost = 0, 0, 0.0

    for path in sorted(folder.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        truth = labels.get(data["filename"])
        if truth is None:
            continue
        if data.get("error"):
            errors += 1
            continue

        latencies.append(data.get("latency_s", 0))
        usage = data.get("usage") or {}
        prompt_tokens += usage.get("prompt_tokens", 0)
        completion_tokens += usage.get("completion_tokens", 0)
        reported_cost += usage.get("cost", 0) or 0

        all_core_ok = True
        for f in SCORED_FIELDS:
            want = normalize_field(f, truth.get(f))
            got = normalize_field(f, canonicalize(data.get("fields")).get(f))
            ok = want == got
            per_field[f].append(ok)
            # "ใกล้เคียง" = ผิดไม่เกิน ~15% ของความยาว ซึ่ง fuzzy search ยังหาเจอ
            near[f].append(ok or (bool(want) and cer(want, got) <= 0.2))
            cers[f].append(cer(want, got))
            if f in CORE_FIELDS and not ok:
                all_core_ok = False
        records += all_core_ok

    total = sum(len(v) for v in per_field.values()) // len(SCORED_FIELDS) or 1
    price_in, price_out = MODELS.get(model, (0.0, 0.0))
    est_cost = (prompt_tokens * price_in + completion_tokens * price_out) / 1_000_000

    return {
        "model": model,
        "variant": variant,
        "n": total,
        "errors": errors,
        "field_acc": {f: sum(v) / len(v) if v else 0.0 for f, v in per_field.items()},
        "mean_acc": statistics.mean(
            [sum(v) / len(v) for f, v in per_field.items() if v and f in CORE_FIELDS] or [0]
        ),
        "name_cer": statistics.mean(cers["name"] or [1]),
        "near_acc": {f: sum(v) / len(v) if v else 0.0 for f, v in near.items()},
        "record_exact": records / total if total else 0.0,
        "cost_per_1000": (reported_cost or est_cost) / total * 1000 if total else 0.0,
        "p50_latency": statistics.median(latencies) if latencies else 0.0,
        "p95_latency": max(latencies) if latencies else 0.0,
    }


def fmt_pct(x: float) -> str:
    return f"{x * 100:.0f}%"


def main() -> None:
    labels = {r["filename"]: r for r in json.loads((ROOT / "example" / "label.json").read_text(encoding="utf-8"))}

    rows = [
        r
        for model in MODELS
        for variant in VARIANTS
        if (r := score_run(labels, model, variant))
    ]
    if not rows:
        print("ยังไม่มีผลใน bench/out — รัน bench/run_bench.py ก่อน")
        return

    rows.sort(key=lambda r: (-r["mean_acc"], r["cost_per_1000"]))

    lines = [
        "# ผลวัด accuracy: model × preprocessing",
        "",
        f"ชุดทดสอบ: {rows[0]['n']} ใบ จาก `example/` เทียบกับ `example/label.json` (normalize ก่อนเทียบทุก field)",
        "",
        "| อันดับ | model | variant | เฉลี่ย core | ถูกทั้งใบ | ชื่อ | ชื่อ≈ | เบอร์ | วันที่ | ทะเบียน | ทะเบียน≈ | ยี่ห้อ | ประเภท | ที่จอด | CER ชื่อ | $/1000 ใบ | p50 | error |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for i, r in enumerate(rows, 1):
        fa = r["field_acc"]
        lines.append(
            f"| {i} | `{r['model']}` | {r['variant']} | **{fmt_pct(r['mean_acc'])}** | {fmt_pct(r['record_exact'])} | "
            f"{fmt_pct(fa['name'])} | {fmt_pct(r['near_acc']['name'])} | {fmt_pct(fa['tel'])} | {fmt_pct(fa['date'])} | "
            f"{fmt_pct(fa['noplate'])} | {fmt_pct(r['near_acc']['noplate'])} | "
            f"{fmt_pct(fa['brand'])} | {fmt_pct(fa['typecar'])} | {fmt_pct(fa['location'])} | "
            f"{r['name_cer']:.2f} | ${r['cost_per_1000']:.2f} | {r['p50_latency']:.1f}s | {r['errors']} |"
        )

    # สรุปว่า preprocessing ช่วยจริงไหม โดยเฉลี่ยข้าม model
    lines += ["", "## preprocessing ช่วยไหม (เฉลี่ยทุก model)", "", "| variant | เฉลี่ย core | ถูกทั้งใบ |", "|---|---|---|"]
    for v in VARIANTS:
        vr = [r for r in rows if r["variant"] == v]
        if vr:
            lines.append(
                f"| {v} | {fmt_pct(statistics.mean(r['mean_acc'] for r in vr))} "
                f"| {fmt_pct(statistics.mean(r['record_exact'] for r in vr))} |"
            )

    best = rows[0]
    cheap = min(rows, key=lambda r: r["cost_per_1000"] / max(r["mean_acc"], 0.01))
    best_variant = max(VARIANTS, key=lambda v: statistics.mean(
        [r["mean_acc"] for r in rows if r["variant"] == v] or [0]))
    lines += [
        "",
        "## สรุปที่ควรใช้",
        "",
        f"- **ตัวที่แม่นที่สุด**: `{best['model']}` + `{best['variant']}` — เฉลี่ย {fmt_pct(best['mean_acc'])}, "
        f"${best['cost_per_1000']:.2f}/1000 ใบ, {best['p50_latency']:.1f}s ต่อใบ",
        f"- **คุ้มที่สุด (acc ต่อราคา)**: `{cheap['model']}` + `{cheap['variant']}` — "
        f"เฉลี่ย {fmt_pct(cheap['mean_acc'])}, ${cheap['cost_per_1000']:.2f}/1000 ใบ",
        f"- **preprocessing ที่ดีที่สุด**: `{best_variant}`",
        "",
        "หมายเหตุ: คอลัมน์ `ชื่อ≈` / `ทะเบียน≈` คือกรณีที่อ่านผิดไม่เกิน ~1-2 ตัวอักษร "
        "ซึ่งยังค้นเจอได้ด้วย fuzzy search และคนตรวจแก้ได้ง่าย — เป็นตัวเลขที่สะท้อนการใช้งานจริงมากกว่า exact match",
    ]

    report = "\n".join(lines) + "\n"
    (ROOT / "bench" / "report.md").write_text(report, encoding="utf-8")
    print(report)


if __name__ == "__main__":
    main()
