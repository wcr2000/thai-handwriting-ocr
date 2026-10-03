"""Compare strategies for locating the slip before reading it, to see which reads most accurately.

The strategies compared (identical model throughout; only the image sent differs):
    full     send the whole frame, no crop at all
    cv       crop with OpenCV, as the production code does
    cv_fb    as cv, but fall back to full's result when no key field could be read (current behaviour)
    llm_box  have an LLM find the 4 corners first, warp to those, then read (two API calls)

The test set comes from two human-labelled sources, never from any model in this comparison:
    example/label.json        the sample slips labelled at the start of the project
    staff-approved slips      values confirmed or corrected by reviewers in the live system

    python bench/crop_bench.py            # run it (cached, so a re-run costs nothing again)
    python bench/crop_bench.py --report   # emit the report only
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import cv2
import httpx
from rapidfuzz.distance import Levenshtein

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bench.detect import detect_quad
from ocrslip.config import OCR_MODEL
from ocrslip.imageio import encode_jpeg, load_image, to_bgr, to_pil
from ocrslip.normalize import normalize_field
from ocrslip.ocr import read_slip
from bench.deskew import deskew
from ocrslip.preprocess import landscape, preprocess, warp_quad
from ocrslip.schema import canonicalize

ROOT = Path(__file__).resolve().parent.parent
CASES = ROOT / "bench" / "crop_cases"      # the test set: images plus ground truth
OUT = ROOT / "bench" / "crop_out"
METHODS = ("full", "cv", "cv_deskew", "llm_box")   # cv_fb is derived from cv + full, needing no extra calls
FIELDS = ["name", "tel", "date", "noplate", "brand", "typecar", "location"]
CORE = ["name", "tel", "noplate"]          # the fields actually used to find a car again


def variants(path: Path, model: str, client: httpx.Client) -> dict[str, dict]:
    """Build the image for each strategy. Returns {method: {"jpeg":..., "extra_calls":..., ...}}"""
    pre = preprocess(path.read_bytes())
    raw_jpeg = encode_jpeg(pre.raw)
    out = {
        "full": {"jpeg": raw_jpeg, "detect_cost": 0.0, "detect_s": 0.0, "found": None},
        "cv": {"jpeg": encode_jpeg(pre.cropped), "detect_cost": 0.0, "detect_s": 0.0,
               "found": pre.quad_found},
        "cv_deskew": {"jpeg": encode_jpeg(to_pil(deskew(to_bgr(pre.cropped)))),
                      "detect_cost": 0.0, "detect_s": 0.0, "found": pre.quad_found},
    }

    quad, usage, secs = detect_quad(raw_jpeg, pre.raw.size, model, client)
    if quad is None:
        jpeg, found = raw_jpeg, False
    else:
        bgr = to_bgr(pre.raw)
        jpeg, found = encode_jpeg(to_pil(landscape(warp_quad(bgr, quad)))), True
    out["llm_box"] = {"jpeg": jpeg, "detect_cost": (usage or {}).get("cost") or 0.0,
                      "detect_s": secs, "found": found}
    return out


def run(model: str, workers: int, force: bool) -> None:
    cases = json.loads((CASES / "cases.json").read_text(encoding="utf-8"))
    client = httpx.Client(timeout=180)

    def one(case: dict) -> str:
        path = CASES / case["file"]
        todo = [m for m in METHODS
                if force or not (OUT / m / f"{path.stem}.json").exists()]
        if not todo:
            return f"skipped {path.stem}"
        built = variants(path, model, client)
        for m in todo:
            v = built[m]
            res = read_slip(v["jpeg"], model, client=client)
            dest = OUT / m / f"{path.stem}.json"
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(json.dumps({
                "file": case["file"], "method": m, "fields": res.fields,
                "error": res.error, "found": v["found"],
                "read_s": res.latency_s, "detect_s": round(v["detect_s"], 2),
                "cost": ((res.usage or {}).get("cost") or 0.0) + v["detect_cost"],
                "size": list(load_image(v["jpeg"]).size),
            }, ensure_ascii=False, indent=2), encoding="utf-8")
        return f"ok {path.stem} ({', '.join(todo)})"

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for i, line in enumerate(pool.map(one, cases), 1):
            print(f"[{i}/{len(cases)}] {line}", flush=True)
    client.close()


def _load(method: str, stem: str) -> dict | None:
    p = OUT / method / f"{stem}.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def _read_something(fields: dict) -> bool:
    return any((fields.get(f) or "") for f in CORE)


def cer(truth: str, got: str) -> float:
    """Character error rate: 0 = exact, 1 = entirely wrong"""
    if not truth:
        return 0.0 if not got else 1.0
    return min(1.0, Levenshtein.distance(truth, got) / len(truth))


def score(cases: list[dict]) -> dict[str, dict]:
    stats: dict[str, dict] = {}
    for method in (*METHODS, "cv_fb"):
        per_field = {f: [] for f in FIELDS}
        near_field = {f: [] for f in FIELDS}
        cer_field = {f: [] for f in FIELDS}
        core_all, costs, secs, rescued, no_crop = [], [], [], 0, 0
        for case in cases:
            stem = Path(case["file"]).stem
            src = "cv" if method == "cv_fb" else method
            data = _load(src, stem)
            if data is None or data.get("error"):
                continue
            fields = canonicalize(data["fields"])
            cost, sec = data["cost"], data["read_s"] + data["detect_s"]

            if method == "cv_fb" and not _read_something(fields):
                alt = _load("full", stem)
                if alt and not alt.get("error"):
                    fields = canonicalize(alt["fields"])
                    cost += alt["cost"]
                    sec += alt["read_s"]
                    rescued += 1
            if data.get("found") is False:
                no_crop += 1

            ok_core = True
            for f in FIELDS:
                want = normalize_field(f, case["truth"].get(f))
                got = normalize_field(f, fields.get(f))
                e = cer(want, got)
                per_field[f].append(want == got)
                # "near" = wrong by no more than ~20% of the length, which fuzzy search still
                # finds and a reviewer corrects quickly. Far more discriminating than exact
                # match when comparing crop strategies.
                near_field[f].append(want == got or (bool(want) and e <= 0.2))
                cer_field[f].append(e)
                if f in CORE and want != got:
                    ok_core = False
            core_all.append(ok_core)
            costs.append(cost)
            secs.append(sec)

        n = len(core_all) or 1
        stats[method] = {
            "n": len(core_all),
            "field": {f: sum(v) / len(v) if v else 0.0 for f, v in per_field.items()},
            "near": {f: sum(v) / len(v) if v else 0.0 for f, v in near_field.items()},
            "cer": {f: statistics.mean(v) if v else 1.0 for f, v in cer_field.items()},
            "core_mean": statistics.mean([sum(per_field[f]) / len(per_field[f])
                                          for f in CORE if per_field[f]] or [0]),
            "core_near": statistics.mean([sum(near_field[f]) / len(near_field[f])
                                          for f in CORE if near_field[f]] or [0]),
            "core_cer": statistics.mean([statistics.mean(cer_field[f])
                                         for f in CORE if cer_field[f]] or [1]),
            "core_all": sum(core_all) / n,
            "cost_1000": statistics.mean(costs or [0]) * 1000,
            "p50_s": statistics.median(secs or [0]),
            "rescued": rescued,
            "no_crop": no_crop,
        }
    return stats


def report(cases: list[dict]) -> str:
    stats = score(cases)
    near = sum(1 for c in cases if c.get("style") == "ใกล้")
    lines = [
        "# Comparing crop strategies: locating the slip before reading it",
        "",
        f"Test set of {len(cases)} slips — {near} shot close up, {len(cases) - near} shot from a distance",
        f"Identical model throughout: `{OCR_MODEL}`. Only the image sent differs.",
        "",
        "The primary metric is **CER** (the share of characters read wrongly; lower is better), "
        "because exact match is far too coarse for Thai handwriting — most reads are wrong by only "
        "1-2 characters, which fuzzy search still finds and a reviewer corrects quickly.",
        "",
        "| strategy | core CER | near | exact | all 3 fields | name≈ | phone | plate≈ | $/1000 slips | p50 | notes |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    label = {"full": "full frame, no crop", "cv": "OpenCV crop",
             "cv_deskew": "OpenCV crop + deskew", "cv_fb": "OpenCV, falling back to full frame",
             "llm_box": "LLM finds the quad, then crop"}
    for m in ("full", "cv", "cv_deskew", "cv_fb", "llm_box"):
        s = stats[m]
        f = s["field"]
        note = []
        if s["no_crop"]:
            note.append(f"quad not found on {s['no_crop']} slips")
        if s["rescued"]:
            note.append(f"fell back to the full frame on {s['rescued']} slips")
        lines.append(
            f"| {label[m]} | **{s['core_cer']:.3f}** | {s['core_near']*100:.0f}% | "
            f"{s['core_mean']*100:.0f}% | {s['core_all']*100:.0f}% | "
            f"{s['near']['name']*100:.0f}% | {f['tel']*100:.0f}% | "
            f"{s['near']['noplate']*100:.0f}% | "
            f"${s['cost_1000']:.2f} | {s['p50_s']:.1f}s | {', '.join(note) or '-'} |"
        )

    for style in ("ใกล้", "ไกล"):
        subset = [c for c in cases if c.get("style") == style]
        if not subset:
            continue
        sub = score(subset)
        style_en = "close up" if style == "ใกล้" else "from a distance"
        lines += ["", f"## Slips shot {style_en} only ({len(subset)} slips)", "",
                  "| strategy | core CER | near | exact | all 3 fields |",
                  "|---|---|---|---|---|"]
        for m in ("full", "cv", "cv_deskew", "cv_fb", "llm_box"):
            t = sub[m]
            lines.append(f"| {label[m]} | **{t['core_cer']:.3f}** | {t['core_near']*100:.0f}% "
                         f"| {t['core_mean']*100:.0f}% | {t['core_all']*100:.0f}% |")
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=OCR_MODEL)
    ap.add_argument("--workers", type=int, default=5)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--report", action="store_true", help="emit the report only, making no further calls")
    args = ap.parse_args()

    cases = json.loads((CASES / "cases.json").read_text(encoding="utf-8"))
    if not args.report:
        run(args.model, args.workers, args.force)
    text = report(cases)
    (ROOT / "bench" / "crop_report.md").write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
