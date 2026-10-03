"""Send the sample images through several models x several preprocessing variants, storing the
raw results for score.py.

Results are cached on disk per (model, variant, file), so a re-run skips what already exists
and nothing is paid for twice.
    python bench/run_bench.py                 # run the whole matrix
    python bench/run_bench.py --models google/gemini-3.8-flash --variants v2
"""

from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ocrslip.imageio import encode_jpeg
from ocrslip.ocr import read_slip
from ocrslip.preprocess import preprocess

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "example"
OUT = ROOT / "bench" / "out"

# USD per 1M tokens (prompt/completion), taken from OpenRouter — used to compute cost per 1000 slips
MODELS: dict[str, tuple[float, float]] = {
    "google/gemini-3.8-flash": (0.75, 3.75),
    "google/gemini-3.7-flash": (0.75, 3.75),
    "google/gemini-3.6-flash": (0.75, 3.75),
    "google/gemini-3.5-flash": (1.50, 9.00),
    "google/gemini-3.1-pro-preview": (2.00, 12.00),
    "google/gemini-3.1-flash-lite": (0.25, 1.50),
    "google/gemini-3-flash-preview": (0.50, 3.00),
    "google/gemini-2.5-pro": (1.25, 10.00),
    "google/gemini-2.5-flash": (0.30, 2.50),
    "anthropic/claude-opus-5.5": (4.00, 20.00),
    "qwen/qwen3-vl-235b-a22b-instruct": (0.21, 1.90),
}

VARIANTS = ("v0_raw", "v1_crop", "v2_enhanced")


def slug(model: str) -> str:
    return model.replace("/", "__")


def build_variants(path: Path) -> dict[str, bytes]:
    """Preprocess one image into a JPEG per variant (done once, reused across every model)"""
    r = preprocess(path)
    return {
        "v0_raw": encode_jpeg(r.raw),
        "v1_crop": encode_jpeg(r.cropped),
        "v2_enhanced": encode_jpeg(r.enhanced),
    }


def _needs_run(cached: Path) -> bool:
    """Re-run when there is no result yet, or the cached result is an error (so an error cannot stick forever)"""
    if not cached.exists():
        return True
    try:
        return json.loads(cached.read_text(encoding="utf-8")).get("error") is not None
    except json.JSONDecodeError:
        return True


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", nargs="*", default=list(MODELS))
    ap.add_argument("--variants", nargs="*", default=list(VARIANTS))
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--force", action="store_true", help="re-run even when a cached result exists")
    args = ap.parse_args()

    images = sorted(p for p in EXAMPLES.iterdir() if p.name.startswith("IMG_"))
    print(f"preparing {len(images)} images ...", flush=True)
    variants_by_image = {p: build_variants(p) for p in images}

    jobs = [
        (model, variant, path)
        for model in args.models
        for variant in args.variants
        for path in images
        if args.force or _needs_run(OUT / slug(model) / variant / f"{path.stem}.json")
    ]
    done = len(args.models) * len(args.variants) * len(images) - len(jobs)
    print(f"{len(jobs)} calls to make ({done} skipped from cache)", flush=True)

    client = httpx.Client(timeout=180)

    def run(job) -> str:
        model, variant, path = job
        res = read_slip(variants_by_image[path][variant], model, client=client)
        dest = OUT / slug(model) / variant / f"{path.stem}.json"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(
            json.dumps(
                {
                    "model": model, "variant": variant, "filename": path.name,
                    "fields": res.fields, "confidence": res.confidence,
                    "latency_s": res.latency_s, "usage": res.usage, "error": res.error,
                },
                ensure_ascii=False, indent=2,
            ),
            encoding="utf-8",
        )
        return f"{'!! ' if res.error else 'ok '} {model:42} {variant:12} {path.stem} {res.latency_s:6.1f}s {res.error or ''}"

    try:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            for i, line in enumerate(pool.map(run, jobs), 1):
                print(f"[{i}/{len(jobs)}] {line}", flush=True)
    finally:
        client.close()


if __name__ == "__main__":
    main()
