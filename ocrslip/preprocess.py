"""Slip preprocessing: find the paper corners -> warp -> rotate to landscape -> adjust tone.

The inputs are phone photos: a small slip lying on wildly varying surfaces (orange
wooden tables, off-white tables and walls), often shot rotated 90/180 degrees, and
filled in with faint blue ballpoint — so heavy binarization is forbidden, because the
pen strokes disappear with it.

Paper-edge detection runs several methods at once and scores them, because no single
method survives every background. When no candidate is trustworthy enough we return
None and let the caller use the full frame instead: a wrong crop is more dangerous
than no crop, because the image then gets skewed, rotated, or clipped with nobody
the wiser.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from .imageio import load_image, to_bgr, to_pil

# Version number of the paper-detection method — bump it on every logic change that
# alters the resulting crop.
#
# reprocess uses this number to decide which slips need redoing, in pure SQL, without
# fetching a single image. The previous approach re-cropped every slip and compared
# sizes, which meant pulling every original image in the database across the network
# (gigabytes of it) — the connection dropped before the job even got going.
PREPROCESS_VERSION = 4


@dataclass
class PreprocessResult:
    raw: Image.Image          # V0 - downscale only
    cropped: Image.Image      # V1 - crop the paper + warp + lay out landscape
    enhanced: Image.Image     # V2 - V1 + shadow removal + CLAHE + sharpen
    quad_found: bool          # whether the paper edges were found (if not, cropped = raw)


def _order_quad(pts: np.ndarray) -> np.ndarray:
    """Order the 4 corners as [top-left, top-right, bottom-right, bottom-left].

    Ordered by angle around the centroid rather than by the min/max of coordinate
    sums and differences: that approach can pick the same point twice when the
    quadrilateral is steeply tilted, leaving a 3-corner quad that warps to mush.
    """
    pts = pts.reshape(4, 2).astype(np.float32)
    center = pts.mean(axis=0)
    # The y axis points down, so sorting by increasing angle yields clockwise order
    pts = pts[np.argsort(np.arctan2(pts[:, 1] - center[1], pts[:, 0] - center[0]))]
    return np.roll(pts, -int(np.argmin(pts.sum(axis=1))), axis=0)


# Shape of a real slip: long/short side ratio around 2.1.
# Size, by contrast, is useless as a criterion, because people shoot from different
# distances — measured against real photos it ranges from 5% to 65% of the frame. So
# the range is left wide and the decision is made on other evidence instead.
SLIP_AR = 2.1
AR_RANGE = (1.45, 3.4)
AREA_RANGE = (0.02, 0.70)
MIN_RECTANGULARITY = 0.7    # the convex hull must fill this much of its minAreaRect, else it is not rectangular paper

# Minimum share of ink pixels inside the region about to be cropped. Filled-in slips
# measure 0.040-0.108, while non-slips (cloth, bare surfaces) measure 0.002 — so this
# is set low, just enough to reject the obviously blank.
MIN_INK = 0.02

# Minimum count of character-sized ink blobs — evidence that does not depend on how
# close the photo was taken. A filled-in slip counts in the hundreds, while the corner
# of a blank paper stack counts 8 (just the seams between sheets).
MIN_CHARS = 40
CHAR_HALF = 150   # blob count scoring half credit — a full slip scores 400-700, a wood chip around one hundred

# Grow the quad outward from its centre before warping, to stop edge text being
# clipped. Taking in a little background is harmless; clipping text loses data.
PAPER_GROW = 0.06     # colour mask: already lands on the paper edge, so only pad where shadow kept the mask short
TEXTURE_GROW = 0.14   # texture mask: only catches the inked area inside the printed frame, so it needs more padding


def paper_mask(bgr: np.ndarray) -> np.ndarray:
    """Colour mask: bright and nearly colourless — works when the background is saturated (orange wooden table).

    White and grey backgrounds pass this mask too, so it must always be paired with
    texture_mask.
    """
    hsv = cv2.cvtColor(cv2.GaussianBlur(bgr, (7, 7), 0), cv2.COLOR_BGR2HSV)
    s, v = hsv[:, :, 1], hsv[:, :, 2]

    # Let Otsu derive the threshold from the image's own distribution rather than hardcoding a constant
    v_thr, _ = cv2.threshold(v, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    s_thr, _ = cv2.threshold(s, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    mask = ((v >= max(v_thr, 110)) & (s <= max(s_thr * 0.9, 60))).astype(np.uint8) * 255
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((15, 15), np.uint8))
    return cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((9, 9), np.uint8))


def ink_mask(bgr: np.ndarray) -> np.ndarray:
    """Pixels clearly darker than their surroundings = printed rules + handwriting.

    Compared against a median-blur background rather than a constant, so it is not
    defeated by shadows or cream-coloured paper.
    """
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY).astype(np.int16)
    bg = cv2.medianBlur(cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY), 31).astype(np.int16)
    return ((bg - gray) > 28).astype(np.uint8)


def texture_mask(bgr: np.ndarray, close_px: float) -> np.ndarray:
    """Texture mask: the slip carries printed dotted rules + handwriting, while tables and walls are smooth.

    Works even when the background is as white as the paper, because it separates on
    roughness rather than brightness. close_px is the distance across which separate
    ink marks may be joined into one blob.
    """
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    # Spread between local max and min in a small window — high where there are strokes, low on smooth surfaces
    k = np.ones((9, 9), np.uint8)
    detail = cv2.subtract(cv2.dilate(gray, k), cv2.erode(gray, k))
    thr, _ = cv2.threshold(detail, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    mask = (detail >= max(thr, 24)).astype(np.uint8) * 255

    # The rules on the slip sit far apart; join them into one blob first, then drop small specks
    close = max(int(close_px) | 1, 9)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((close, close), np.uint8))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((9, 9), np.uint8))
    return mask


def texture_masks(bgr: np.ndarray) -> list[np.ndarray]:
    """texture_mask at several scales, because the shooting distance is not known in advance.

    A join distance tuned for a slip filling the frame is far too large for one shot
    from a distance: it welds the slip to the surrounding wood grain into a single
    blob, and the shape then distorts enough to fail the criteria.
    """
    long_side = max(bgr.shape[:2])
    return [texture_mask(bgr, long_side * f) for f in (0.015, 0.03, 0.05)]


def _expand_quad(quad: np.ndarray, frac: float, shape: tuple[int, int]) -> np.ndarray:
    """Grow the quad outward from its centre by the given fraction, clamped to the image bounds"""
    if frac <= 0:
        return quad
    center = quad.mean(axis=0)
    grown = center + (quad - center) * (1.0 + frac)
    h, w = shape
    grown[:, 0] = np.clip(grown[:, 0], 0, w - 1)
    grown[:, 1] = np.clip(grown[:, 1], 0, h - 1)
    return grown.astype(np.float32)


def _quad_from_contour(c: np.ndarray) -> np.ndarray:
    """contour -> 4 corners. Try approx first (more accurate perspective), fall back to minAreaRect"""
    peri = cv2.arcLength(c, True)
    for eps in (0.02, 0.03, 0.04, 0.06):
        approx = cv2.approxPolyDP(c, eps * peri, True)
        if len(approx) == 4 and cv2.isContourConvex(approx):
            quad = approx.astype(np.float32).reshape(4, 2)
            # Guard against an approx so distorted that it swallows area off the paper
            if cv2.contourArea(quad) <= 1.25 * cv2.contourArea(c):
                return quad
            break
    return cv2.boxPoints(cv2.minAreaRect(c)).astype(np.float32).reshape(4, 2)


def _score_quad(
    quad: np.ndarray, hull: np.ndarray, area_total: float, chars: int = 0
) -> float | None:
    """Score how much this quad "looks like a parking slip". Returns None if it fails the criteria.

    The criteria derive from the shape of a real slip, not from the background colour,
    so they hold wherever the photo was taken.
    """
    (_, _), (w, h), _ = cv2.minAreaRect(hull)
    if min(w, h) < 1:
        return None
    ar = max(w, h) / min(w, h)
    if not AR_RANGE[0] <= ar <= AR_RANGE[1]:
        return None

    quad_area = cv2.contourArea(quad)
    if not AREA_RANGE[0] * area_total <= quad_area <= AREA_RANGE[1] * area_total:
        return None

    rectangularity = cv2.contourArea(hull) / max(w * h, 1)
    if rectangularity < MIN_RECTANGULARITY:
        return None

    # The closer the aspect ratio is to a real slip, the more fully rectangular it is,
    # and the more text sits inside it, the higher the score.
    #
    # The character count matters more than you would expect: a small chip of wood grain
    # at exactly 2:1 once beat the actual slip, even though it counted 116 ink blobs
    # against the slip's 441 — shape alone cannot tell those two apart.
    ar_fit = 1.0 / (1.0 + abs(np.log(ar / SLIP_AR)) * 3)
    char_fit = chars / (chars + CHAR_HALF)
    return float(ar_fit * rectangularity * char_fit)


def ink_evidence(quad: np.ndarray, ink: np.ndarray) -> tuple[float, int]:
    """Evidence that this quad really holds filled-in text. Returns (ink share, count of character-sized blobs).

    The blob count is evidence independent of shooting distance, because it is measured
    against the size of the quad itself rather than the image — the same slip shot near
    or far yields a similar count.
    """
    region = np.zeros(ink.shape, np.uint8)
    cv2.fillConvexPoly(region, quad.astype(np.int32), 1)
    inside = ink * region
    area = max(int(region.sum()), 1)

    side = np.sqrt(area)
    n, _, stats, _ = cv2.connectedComponentsWithStats(inside, 8)
    chars = sum(
        1 for i in range(1, n)
        # Not so small as to be noise, and not so long as to be a rule line or paper edge
        if 0.005 * side < max(stats[i, 2], stats[i, 3]) < 0.25 * side and stats[i, 4] > 4
    )
    return float(inside.sum() / area), chars


def _quad_candidates(
    mask: np.ndarray, shape: tuple[int, int], ink: np.ndarray, *, grow: float = 0.0,
) -> list[tuple[float, np.ndarray]]:
    """Every blob in the mask that passes the shape criteria, with its score.

    Decided on the blob's convex hull, because paper is a convex shape, and the notches
    and holes left by the gaps between characters should not count against its
    rectangularity.
    """
    area_total = float(shape[0] * shape[1])
    out: list[tuple[float, np.ndarray]] = []
    n, labels, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    for i in range(1, n):
        if not AREA_RANGE[0] * area_total < stats[i, cv2.CC_STAT_AREA] < AREA_RANGE[1] * area_total:
            continue
        blob = (labels == i).astype(np.uint8) * 255
        contours, _ = cv2.findContours(blob, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not contours:
            continue
        hull = cv2.convexHull(max(contours, key=cv2.contourArea))
        quad = _expand_quad(_quad_from_contour(hull), grow, shape)

        # A filled-in slip must have text inside it. This keeps us off cloth, smooth
        # surfaces and blank paper that happen to pass the shape criteria. It replaces
        # a size limit, which cannot work because people shoot from different distances.
        ratio, chars = ink_evidence(quad, ink)
        if ratio < MIN_INK or chars < MIN_CHARS:
            continue

        score = _score_quad(quad, hull, area_total, chars)
        if score is not None:
            out.append((score, quad))
    return out


def find_paper_quad(bgr: np.ndarray) -> np.ndarray | None:
    """Find the paper's 4 corners. Returns None when unsure, so the caller falls back to the full frame.

    Tries both the colour mask and the texture mask, then picks whichever looks most
    like a parking slip.
    """
    shape = bgr.shape[:2]
    ink = ink_mask(bgr)
    candidates = _quad_candidates(paper_mask(bgr), shape, ink, grow=PAPER_GROW)
    for mask in texture_masks(bgr):
        candidates += _quad_candidates(mask, shape, ink, grow=TEXTURE_GROW)
    if not candidates:
        return None

    return _order_quad(max(candidates, key=lambda t: t[0])[1])


def warp_quad(bgr: np.ndarray, quad: np.ndarray, pad: int = 12) -> np.ndarray:
    """Perspective-warp the paper flat (with a small margin so text is not clipped)"""
    tl, tr, br, bl = quad
    width = int(max(np.linalg.norm(tr - tl), np.linalg.norm(br - bl)))
    height = int(max(np.linalg.norm(bl - tl), np.linalg.norm(br - tr)))
    width, height = max(width, 32), max(height, 32)
    dst = np.array(
        [[pad, pad], [width + pad, pad], [width + pad, height + pad], [pad, height + pad]],
        dtype=np.float32,
    )
    m = cv2.getPerspectiveTransform(quad, dst)
    return cv2.warpPerspective(
        bgr, m, (width + 2 * pad, height + 2 * pad), flags=cv2.INTER_CUBIC,
        borderMode=cv2.BORDER_REPLICATE,
    )


def landscape(bgr: np.ndarray) -> np.ndarray:
    """Rotate so the long side runs horizontally (slips are landscape), leaving only 0 vs 180 degrees ambiguous"""
    h, w = bgr.shape[:2]
    return cv2.rotate(bgr, cv2.ROTATE_90_CLOCKWISE) if h > w else bgr


def upright(img: Image.Image, orientation: str) -> Image.Image:
    """Rotate 180 degrees when the model reports the slip is upside down; return it unchanged otherwise.

    landscape() only resolves the 90-degree case, leaving 0 vs 180 ambiguous — and the
    shape of the paper cannot settle that. You have to read the text, which the model
    is already doing, so we ask it to tell us instead of guessing ourselves.
    """
    return img.transpose(Image.ROTATE_180) if orientation == "upside_down" else img


def enhance(bgr: np.ndarray) -> np.ndarray:
    """Remove shadows and lift contrast gently. No binarization, which would erase blue ballpoint."""
    # Flatten shadows and uneven lighting by dividing out a median-blur background
    bg = cv2.medianBlur(bgr, 31)
    flat = cv2.divide(bgr, bg, scale=192)

    lab = cv2.cvtColor(flat, cv2.COLOR_BGR2LAB)
    l, a, b = cv2.split(lab)
    l = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8)).apply(l)
    out = cv2.cvtColor(cv2.merge((l, a, b)), cv2.COLOR_LAB2BGR)

    # A light unsharp pass to crisp up the pen strokes
    blur = cv2.GaussianBlur(out, (0, 0), 2.0)
    return cv2.addWeighted(out, 1.5, blur, -0.5, 0)


def preprocess(source: str | Path | bytes) -> PreprocessResult:
    raw_pil = load_image(source)
    bgr = to_bgr(raw_pil)

    quad = find_paper_quad(bgr)
    # Only rotate to landscape when the crop succeeded, because then we know for certain
    # that what we have is the slip. Rotating the whole frame after a failed detection
    # would turn a squarely-shot slip sideways instead.
    cropped = landscape(warp_quad(bgr, quad)) if quad is not None else bgr

    return PreprocessResult(
        raw=raw_pil,
        cropped=to_pil(cropped),
        enhanced=to_pil(enhance(cropped)),
        quad_found=quad is not None,
    )


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="preprocess parking slips and write the results as JPEG files")
    ap.add_argument("images", nargs="+")
    ap.add_argument("-o", "--out", default="out/preprocess")
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for path in args.images:
        stem = Path(path).stem
        r = preprocess(path)
        r.cropped.save(out / f"{stem}.v1_crop.jpg", quality=90)
        r.enhanced.save(out / f"{stem}.v2_enhanced.jpg", quality=90)
        print(f"{stem:14} quad={'ok  ' if r.quad_found else 'MISS'} crop={r.cropped.size}")


if __name__ == "__main__":
    main()
