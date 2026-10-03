"""The whole chain: uploaded file -> preprocess -> OCR -> decide if review is needed -> save to the DB"""

from __future__ import annotations

from typing import Any

import psycopg
from PIL import Image

from ..config import DB_SCHEMA, OCR_MODEL
from ..db import add_image, insert_slip, slip_with_image
from ..imageio import encode_jpeg
from ..normalize import norm_phone, norm_plate
from ..ocr import OcrResult, read_slip
from ..preprocess import PREPROCESS_VERSION, PreprocessResult, preprocess, upright
from ..review import evaluate

# The benchmark says cropping alone beats tone adjustment (71% vs 68%) — lifting contrast
# distorts thin pen strokes.
VARIANT = "v1_crop"


def build_raw_ocr(
    res: OcrResult, quad_found: bool, problems: dict[str, str], **extra: Any
) -> dict[str, Any]:
    """The OCR audit trail kept for later inspection — shared between upload and reprocess.

    orientation, fills_frame and preprocess_version must always be in here, because
    reprocess uses those three to decide which slips need redoing — in pure SQL, without
    loading a single image. Omit them and every freshly uploaded slip gets sent back to
    the model on every run.
    """
    return {
        "fields": res.fields,
        "quad_found": quad_found,
        "problems": problems,
        "orientation": res.orientation,
        "fills_frame": res.fills_frame,
        "preprocess_version": PREPROCESS_VERSION,
        **extra,
    }


def read_with_fallback(
    pre: PreprocessResult, model: str = OCR_MODEL
) -> tuple[OcrResult, Image.Image, bool]:
    """Read from the cropped image; only if that reads nothing, retry with the full frame.

    Returns (result used, image that was read, whether the full frame was used).

    The paper-edge detector will never be right 100% of the time — observed failures
    include locking onto the stack of blank paper next to the slip, and onto the wood
    grain of the table. Once the crop is wrong the model returns null for every field,
    with nobody able to tell why. Retrying with the full frame recovers all of these cases
    in one stroke, with no need to tune the CV for each one, and costs extra only when
    something has actually gone wrong.

    Two signals are used, because a bad crop comes in two shapes:
      - nothing read at all = we locked onto something with no text on it (blank paper, table top)
      - the model reports the slip does not fill most of the frame = so much background was
        taken in that the slip sits small and tilted in a corner, still just about readable
        but less accurately, and hard for a reviewer to look at.
        The second shape cannot be measured from the CV side at all. Every signal was tried
        (size, aspect ratio, rectangularity, ink density, collision with the frame edge,
        colour saturation, combined score) and the values for a correct crop and for one
        that landed on the table overlap completely. The model, meanwhile, got it right on
        13 of 13 slips in the real trial.
    """
    res = read_slip(encode_jpeg(pre.cropped), model)
    if not res.ok or not pre.quad_found:
        return res, pre.cropped, False
    if _read_something(res) and res.fills_frame:
        return res, pre.cropped, False

    retry = read_slip(encode_jpeg(pre.raw), model)
    if retry.ok and _read_something(retry):
        return retry, pre.raw, True
    return res, pre.cropped, False


def _read_something(res: OcrResult) -> bool:
    """Did the model read anything at all? All key fields empty means the image we sent is unusable."""
    return any((res.fields.get(f) or "") for f in ("name", "tel", "noplate"))


def count_duplicates(conn: psycopg.Connection, fields: dict[str, Any]) -> int:
    """Count still-parked slips sharing a plate or phone — catches double entry and the same car re-registered"""
    plate, tel = norm_plate(fields.get("noplate")), norm_phone(fields.get("tel"))
    if not plate and not tel:
        return 0
    cur = conn.execute(
        f"""SELECT count(*) AS n FROM {DB_SCHEMA}.slips
            WHERE car_status = 'stored' AND review_status <> 'rejected'
              AND ((%s <> '' AND plate_norm = %s) OR (%s <> '' AND tel_digits = %s))""",
        (plate, plate, tel, tel),
    )
    return cur.fetchone()["n"]


def ingest(
    conn: psycopg.Connection,
    raw: bytes,
    *,
    created_by: str | None = None,
    uploaded_by: str | None = None,
    photographer: str | None = None,
) -> dict[str, Any]:
    """Process one image, save it as pending, and return a summary for display"""
    pre = preprocess(raw)
    original_jpeg = encode_jpeg(pre.raw, quality=85)

    # The duplicate-upload gate. It has to sit *before* the model call for two reasons: it
    # avoids creating duplicate slips that reviewers then work through for nothing, and it
    # avoids paying for OCR on an image already read. (In production this produced 650
    # surplus slips from repeated submits.)
    #
    # Compared on the hash of the *original* image, not the preprocessed one, because the
    # bytes of the processed image depend on the model's answer (the rotation applied, the
    # decision to retry with the full frame), so the same photo submitted twice often
    # yields different bytes. The earlier gate, which compared processed images, caught
    # only 200 of those 650. The original comes from encode_jpeg(pre.raw), computed purely
    # from the uploaded file, and so is identical every time.
    if (twin := slip_with_image(conn, original_jpeg)) is not None:
        return {
            "ok": True, "duplicate_of": twin, "id": twin["id"],
            "fields": {"name": twin["name"], "tel": twin["tel"], "noplate": twin["plate_raw"]},
            "reasons": [], "problems": {}, "latency_s": 0.0,
        }

    res, used, full_frame = read_with_fallback(pre)
    if not res.ok:
        return {"ok": False, "error": res.error}

    # The model reads an upside-down slip fine, but a human reviewer cannot, so we store
    # the image rotated upright. Rotating after OCR rather than before avoids a second
    # model call.
    cropped = upright(used, res.orientation)
    processed_jpeg = encode_jpeg(cropped)

    fields = {k: v for k, v in res.fields.items()}
    reasons, problems = evaluate(fields, res.confidence, count_duplicates(conn, fields))
    # The original is unique, but the cropped image matches another slip = the same slip
    # photographed twice from different angles. This cannot be hard-blocked (the files
    # genuinely differ), so it is flagged for a reviewer to decide, as before.
    if slip_with_image(conn, processed_jpeg) is not None:
        reasons = sorted({*reasons, "duplicate_image"})

    slip_id = insert_slip(
        conn, fields,
        confidence=res.confidence,
        raw_ocr=build_raw_ocr(res, pre.quad_found and not full_frame, problems),
        review_reason=reasons,
        ocr_model=res.model,
        ocr_variant=VARIANT,
        usage=res.usage,
        latency_s=res.latency_s,
        created_by=created_by,
        uploaded_by=uploaded_by,
        photographer=photographer,
    )
    add_image(conn, slip_id, "processed", processed_jpeg, cropped.size)
    add_image(conn, slip_id, "original", original_jpeg, pre.raw.size)
    conn.commit()

    return {
        "ok": True, "id": slip_id, "fields": fields, "reasons": reasons,
        "problems": problems, "latency_s": res.latency_s,
    }
