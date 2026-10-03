"""Score run_bench.py's results against example/label.json and write bench/report.md.

Every field is normalized before comparison (Thai numerals, name honorifics, brand names in
Thai or English, date formats), so what is measured is "did it read correctly?" rather than
"does it match character for character?"
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
# The fields that must all be right for a slip to count as fully correct. location and province
# are excluded, because many slips simply do not have them filled in.
CORE_FIELDS = ["name", "tel", "date", "noplate", "brand", "typecar"]


def cer(truth: str, got: str) -> float:
    """Character error rate: 0 = exact, 1 = entirely wrong"""
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
            # "near" = wrong by no more than ~15% of the length, which fuzzy search still finds
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
        print("no results in bench/out yet — run bench/run_bench.py first")
        return

    rows.sort(key=lambda r: (-r["mean_acc"], r["cost_per_1000"]))

    lines = [
        "# Accuracy benchmark: model x preprocessing",
        "",
        f"Test set: {rows[0]['n']} slips from `example/`, scored against `example/label.json` "
        f"(every field normalized before comparison)",
        "",
        "| # | model | variant | mean core | whole slip | name | name≈ | phone | date | plate | plate≈ | brand | type | spot | name CER | $/1000 slips | p50 | errors |",
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

    # Does preprocessing actually help? Averaged across every model.
    lines += ["", "## Does preprocessing help? (averaged over all models)", "",
              "| variant | mean core | whole slip |", "|---|---|---|"]
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
        "## What to use",
        "",
        f"- **Most accurate**: `{best['model']}` + `{best['variant']}` — {fmt_pct(best['mean_acc'])} mean, "
        f"${best['cost_per_1000']:.2f} per 1000 slips, {best['p50_latency']:.1f}s per slip",
        f"- **Best value (accuracy per cost)**: `{cheap['model']}` + `{cheap['variant']}` — "
        f"{fmt_pct(cheap['mean_acc'])} mean, ${cheap['cost_per_1000']:.2f} per 1000 slips",
        f"- **Best preprocessing**: `{best_variant}`",
        "",
        "Note: the `name≈` and `plate≈` columns count reads that are wrong by no more than ~1-2 "
        "characters, which fuzzy search still finds and a reviewer corrects easily. They reflect "
        "real-world usability better than exact match does.",
    ]

    report = "\n".join(lines) + "\n"
    (ROOT / "bench" / "report.md").write_text(report, encoding="utf-8")
    print(report)


if __name__ == "__main__":
    main()
