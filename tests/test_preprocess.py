"""Paper-edge detection tests — a regression suite for the bug that cropped onto the background instead of the slip.

The original bug: paper_mask found paper by "bright and colourless", which only works on an
orange wooden table. Photographed on a white table or wall, the background met the same criteria
as the paper, so the system cropped the entire frame — and then landscape() rotated it 90
degrees as well, all while quad_found still reported true.

Every image in this file is synthesised. Never use a real parking slip as a fixture: this
repository is public.
"""

import cv2
import numpy as np
import pytest

from ocrslip.imageio import encode_jpeg, to_pil
import ocrslip.preprocess as P
from ocrslip.preprocess import find_paper_quad, preprocess

FRAME = (2000, 1500)          # (height, width), imitating a portrait phone photo
SLIP = (540, 1150)            # (height, width) of the slip: ~2.1 aspect ratio, ~21% of the frame, as in reality

WOOD = (40, 110, 190)         # BGR orange wooden table — the background the original code handled
WHITE_DESK = (205, 207, 208)  # BGR off-white table or wall — the background that broke the original code


def fake_photo(bg: tuple[int, int, int], angle: float = 0.0) -> np.ndarray:
    """A synthetic photo: a white slip with printed rules and handwriting, tilted on the given background colour"""
    img = np.full((*FRAME, 3), bg, np.uint8)

    slip = np.full((*SLIP, 3), 250, np.uint8)
    for i in range(1, 5):                       # printed rules
        y = i * SLIP[0] // 5
        cv2.line(slip, (60, y), (SLIP[1] - 60, y), (120, 120, 120), 3)
    for i in range(1, 5):                       # blue ballpoint handwriting
        y = i * SLIP[0] // 5
        cv2.putText(slip, "0812345678", (120, y - 14), 0, 1.6, (150, 60, 30), 4)

    return _paste(img, slip, angle)


def _paste(img: np.ndarray, slip: np.ndarray, angle: float) -> np.ndarray:
    """Place the slip at the centre of the frame, rotated by angle degrees"""
    h, w = slip.shape[:2]
    canvas = np.zeros((*FRAME, 3), np.uint8)
    alpha = np.zeros(FRAME, np.uint8)
    y0, x0 = (FRAME[0] - h) // 2, (FRAME[1] - w) // 2
    canvas[y0:y0 + h, x0:x0 + w] = slip
    alpha[y0:y0 + h, x0:x0 + w] = 255

    m = cv2.getRotationMatrix2D((FRAME[1] / 2, FRAME[0] / 2), angle, 1.0)
    canvas = cv2.warpAffine(canvas, m, (FRAME[1], FRAME[0]))
    alpha = cv2.warpAffine(alpha, m, (FRAME[1], FRAME[0]))
    return np.where(alpha[:, :, None] > 127, canvas, img)


def run(bgr: np.ndarray):
    return preprocess(encode_jpeg(to_pil(bgr), quality=95))


@pytest.mark.parametrize("bg,label", [(WOOD, "wooden table"), (WHITE_DESK, "white table")])
@pytest.mark.parametrize("angle", [0.0, 8.0, -12.0])
def test_crop_finds_slip_on_any_background(bg, label, angle):
    """The crop must land on the slip alone whatever the background colour — this is the original bug's regression"""
    r = run(fake_photo(bg, angle))
    assert r.quad_found, f"paper edges not found on a {label} at {angle} degrees"

    w, h = r.cropped.size
    assert w > h, "the result must always be landscape"

    ar = w / h
    assert 1.6 <= ar <= 3.0, f"distorted aspect ratio ({ar:.2f}) means the crop did not land on the slip"

    coverage = (w * h) / (FRAME[0] * FRAME[1])
    assert 0.1 < coverage < 0.45, f"the crop covers {coverage:.0%} of the frame — not the size of the slip"


def test_plain_background_is_not_mistaken_for_paper():
    """A plain white background with no slip must report "not found" and return the full frame rather than crop blindly.

    Letting OCR read the whole frame beats passing on an image that was skewed, rotated or
    clipped with nobody the wiser.
    """
    img = np.full((*FRAME, 3), WHITE_DESK, np.uint8)
    assert find_paper_quad(img) is None

    r = run(img)
    assert not r.quad_found
    assert r.cropped.size == r.raw.size


def test_giant_bright_blob_is_rejected():
    """A bright blob filling nearly the whole frame (a wall, a table) must not be counted as paper"""
    img = np.full((*FRAME, 3), (60, 60, 60), np.uint8)
    cv2.rectangle(img, (20, 20), (FRAME[1] - 20, FRAME[0] - 20), (230, 232, 233), -1)
    assert find_paper_quad(img) is None


def test_blank_paper_nearby_does_not_beat_the_slip():
    """A stack of blank paper nearby that happens to have a slip-like aspect ratio must not beat the real slip.

    Regression: the corner of a blank paper stack covering 3% of the frame once scored better on
    aspect ratio than the slightly tilted slip, so the system cropped onto the blank paper and
    OCR returned null for every field.
    """
    img = fake_photo(WOOD, angle=6.0)
    # Blank paper at 2:1, bottom-left corner, much smaller than the real slip
    cv2.rectangle(img, (60, FRAME[0] - 380), (60 + 360, FRAME[0] - 200), (248, 248, 248), -1)

    r = run(img)
    assert r.quad_found
    w, h = r.cropped.size
    coverage = (w * h) / (FRAME[0] * FRAME[1])
    assert coverage > 0.1, f"the crop covers only {coverage:.1%} of the frame — it landed on the blank paper, not the slip"


@pytest.mark.parametrize("orientation,rotated", [
    ("upside_down", True), ("upright", False), ("", False),
])
def test_upright_rotates_only_when_model_says_upside_down(orientation, rotated):
    """landscape() only resolves the 90-degree case; 0 vs 180 has to be taken from what the model read"""
    img = to_pil(fake_photo(WOOD))
    out = P.upright(img, orientation)
    assert (out is not img) == rotated
    if rotated:
        assert np.array_equal(np.asarray(out), np.asarray(img)[::-1, ::-1])


def test_raw_ocr_always_records_orientation():
    """raw_ocr must always carry orientation.

    reprocess uses this field to decide which slips have "never been checked for being upside
    down". Omit it and every freshly uploaded slip gets sent back to the model on every reprocess
    run.
    """
    from ocrslip.ocr import OcrResult
    from ocrslip.web.pipeline import build_raw_ocr

    res = OcrResult(model="m", fields={"name": "ก"}, confidence={}, latency_s=1.0,
                    orientation="upside_down")
    raw = build_raw_ocr(res, True, {}, reprocessed=True)
    assert raw["orientation"] == "upside_down"
    assert raw["quad_found"] is True
    assert raw["reprocessed"] is True


def test_order_quad_keeps_all_four_corners_when_tilted():
    """Ordering the corners must not leave only 3 of them.

    Regression: the old method ordered by the min/max of x+y and x-y, which can pick the same
    point twice when the quadrilateral is steeply tilted. The resulting quad was malformed and
    warped into a blurred image with nothing in it.
    """
    for angle in range(0, 90, 7):
        rect = cv2.boxPoints(((500.0, 400.0), (600.0, 280.0), float(angle)))
        ordered = P._order_quad(rect.astype(np.float32))
        assert len({tuple(np.round(p, 3)) for p in ordered}) == 4, f"duplicate corner at {angle} degrees"

        tl, tr, br, bl = ordered
        assert tl[0] < tr[0] or tl[1] < bl[1]          # clockwise, starting from top-left
        assert cv2.contourArea(ordered) > 0.9 * 600 * 280


def _ocr(fills_frame=True, **fields):
    from ocrslip.ocr import OcrResult

    return OcrResult(model="m", fields=fields, confidence={}, latency_s=0.1,
                     fills_frame=fills_frame)


def _fake_preprocess(quad_found: bool):
    img = to_pil(fake_photo(WOOD))
    return P.PreprocessResult(raw=img, cropped=img.crop((0, 0, 40, 20)),
                              enhanced=img, quad_found=quad_found)


@pytest.mark.parametrize("quad_found,reads,want_full,want_calls", [
    # Good crop, read correctly first time — must not call again
    (True, [_ocr(name="ก", tel="0812345678", noplate="กก1234")], False, 1),
    # Readable, but the model reports the slip does not fill most of the frame = the crop caught
    # the table top. Try the full frame, even though every field was read.
    (True, [_ocr(fills_frame=False, name="ก", tel="0812345678"),
            _ocr(name="ข", tel="0899999999")], True, 2),
    # Bad crop, nothing read, and the full frame reads fine — use the full-frame result
    (True, [_ocr(), _ocr(name="ก", tel="0812345678")], True, 2),
    # Edges not found at all, so the image sent was already the full frame — a retry would read the same
    (False, [_ocr()], False, 1),
    # The full frame reads nothing either — return the first result rather than making it worse
    (True, [_ocr(), _ocr()], False, 2),
])
def test_read_with_fallback_retries_full_frame_only_when_crop_reads_nothing(
    monkeypatch, quad_found, reads, want_full, want_calls
):
    """The safety net for when the crop lands on the wrong thing.

    There are two signals: nothing read at all (we caught something with no text on it), and the
    model reporting that the slip does not fill most of the frame (so much background was taken
    in that the slip sits small and tilted).
    """
    from ocrslip.web import pipeline

    calls = []
    monkeypatch.setattr(pipeline, "read_slip",
                        lambda jpeg, model: (calls.append(jpeg), reads[len(calls) - 1])[1])

    pre = _fake_preprocess(quad_found)
    res, used, full_frame = pipeline.read_with_fallback(pre)

    assert len(calls) == want_calls
    assert full_frame is want_full
    assert used is (pre.raw if want_full else pre.cropped)
    assert res is reads[len(calls) - 1 if want_full else 0]
