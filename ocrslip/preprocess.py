"""Preprocessing ใบฝากรถ: จับมุมกระดาษ -> warp -> หมุนให้ด้านยาวเป็นแนวนอน -> ปรับสี

ภาพต้นทางเป็นรูปถ่ายมือถือ: สลิปแผ่นเล็กวางบนพื้นหลากหลาย (โต๊ะไม้สีส้ม, โต๊ะ/ผนังสีขาวเทา)
มักถ่ายตะแคง 90/180 องศา เขียนด้วยปากกาน้ำเงินจาง จึงห้าม binarize แรงเพราะเส้นปากกาจะหาย

การหาขอบกระดาษใช้หลายวิธีพร้อมกันแล้วให้คะแนน เพราะวิธีเดียวเอาไม่อยู่ทุกพื้นหลัง
และถ้าไม่มี candidate ไหนน่าเชื่อถือพอ จะคืน None ให้ caller ใช้ภาพเต็มแทน —
crop ผิดอันตรายกว่าไม่ crop เพราะภาพจะถูกบิด/หมุน/ตัดขอบโดยที่ไม่มีใครรู้
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from .imageio import load_image, to_bgr, to_pil


@dataclass
class PreprocessResult:
    raw: Image.Image          # V0 - ย่อขนาดอย่างเดียว
    cropped: Image.Image      # V1 - crop กระดาษ + warp + วางแนวนอน
    enhanced: Image.Image     # V2 - V1 + ลบเงา + CLAHE + sharpen
    quad_found: bool          # หาขอบกระดาษเจอหรือไม่ (ถ้าไม่เจอ cropped = raw)


def _order_quad(pts: np.ndarray) -> np.ndarray:
    """เรียงจุด 4 มุมเป็น [top-left, top-right, bottom-right, bottom-left]"""
    pts = pts.reshape(4, 2).astype(np.float32)
    s = pts.sum(axis=1)
    d = np.diff(pts, axis=1).ravel()
    return np.array(
        [pts[np.argmin(s)], pts[np.argmin(d)], pts[np.argmax(s)], pts[np.argmax(d)]],
        dtype=np.float32,
    )


# รูปร่างของใบฝากรถจริง วัดจากใบที่ crop สำเร็จ: อัตราส่วนด้านยาว/ด้านสั้น 1.96-2.25
# และกินพื้นที่ 20-35% ของเฟรม เผื่อช่วงให้กว้างกว่าที่วัดได้พอสมควร กันใบที่ถ่ายใกล้/ไกลผิดปกติ
SLIP_AR = 2.1
AR_RANGE = (1.45, 3.4)
AREA_RANGE = (0.03, 0.55)
MIN_RECTANGULARITY = 0.8    # convex hull ต้องเต็ม minAreaRect เกินเท่านี้ ไม่งั้นไม่ใช่กระดาษสี่เหลี่ยม

# texture_mask จับได้แค่บริเวณที่มีหมึก ซึ่งอยู่ในกรอบพิมพ์ที่เว้นจากขอบกระดาษเข้ามา
# จึงต้องขยาย quad ออกเล็กน้อยเพื่อดึงขอบกระดาษจริงกลับมา ไม่งั้นตัวหนังสือริมขอบจะโดนตัด
TEXTURE_GROW = 0.12


def paper_mask(bgr: np.ndarray) -> np.ndarray:
    """mask จากสี: สว่าง + แทบไม่มีสี — ใช้ได้เมื่อพื้นหลังอิ่มสี (โต๊ะไม้สีส้ม)

    พื้นหลังขาว/เทาจะหลุด mask นี้มาด้วย จึงต้องมี texture_mask คู่กันเสมอ
    """
    hsv = cv2.cvtColor(cv2.GaussianBlur(bgr, (7, 7), 0), cv2.COLOR_BGR2HSV)
    s, v = hsv[:, :, 1], hsv[:, :, 2]
    # ใช้ Otsu หา threshold เองจากการกระจายของภาพ แทนการ hardcode ค่าคงที่
    v_thr, _ = cv2.threshold(v, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    s_thr, _ = cv2.threshold(s, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    mask = ((v >= max(v_thr, 110)) & (s <= max(s_thr * 0.9, 60))).astype(np.uint8) * 255
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((15, 15), np.uint8))
    return cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((9, 9), np.uint8))


def texture_mask(bgr: np.ndarray) -> np.ndarray:
    """mask จากลวดลาย: ในใบมีเส้นประพิมพ์ + ลายมือ ส่วนโต๊ะ/ผนังเรียบ

    ใช้ได้แม้พื้นหลังจะขาวพอ ๆ กับกระดาษ เพราะแยกด้วย "ความไม่เรียบ" ไม่ใช่ความสว่าง
    """
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    # ความต่างของ local max/min ในหน้าต่างเล็ก ๆ — สูงตรงที่มีเส้น ต่ำตรงพื้นเรียบ
    k = np.ones((9, 9), np.uint8)
    detail = cv2.subtract(cv2.dilate(gray, k), cv2.erode(gray, k))
    thr, _ = cv2.threshold(detail, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    mask = (detail >= max(thr, 24)).astype(np.uint8) * 255

    # เส้นในใบอยู่ห่างกัน ต้องเชื่อมให้เป็นก้อนเดียวก่อน แล้วค่อยลบจุดรบกวนเล็ก ๆ
    long_side = max(bgr.shape[:2])
    close = max(int(long_side * 0.04) | 1, 21)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((close, close), np.uint8))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((9, 9), np.uint8))
    return mask


def _expand_quad(quad: np.ndarray, frac: float, shape: tuple[int, int]) -> np.ndarray:
    """ขยาย quad ออกจากจุดกึ่งกลางตามสัดส่วนที่กำหนด แล้วหนีบไม่ให้เลยขอบภาพ"""
    if frac <= 0:
        return quad
    center = quad.mean(axis=0)
    grown = center + (quad - center) * (1.0 + frac)
    h, w = shape
    grown[:, 0] = np.clip(grown[:, 0], 0, w - 1)
    grown[:, 1] = np.clip(grown[:, 1], 0, h - 1)
    return grown.astype(np.float32)


def _quad_from_contour(c: np.ndarray) -> np.ndarray:
    """contour -> 4 มุม ลอง approx ก่อน (ได้ perspective แม่นกว่า) ไม่ได้ค่อยใช้ minAreaRect"""
    peri = cv2.arcLength(c, True)
    for eps in (0.02, 0.03, 0.04, 0.06):
        approx = cv2.approxPolyDP(c, eps * peri, True)
        if len(approx) == 4 and cv2.isContourConvex(approx):
            quad = approx.astype(np.float32).reshape(4, 2)
            # กันกรณี approx เบี้ยวจนกินพื้นที่นอกกระดาษ
            if cv2.contourArea(quad) <= 1.25 * cv2.contourArea(c):
                return quad
            break
    return cv2.boxPoints(cv2.minAreaRect(c)).astype(np.float32).reshape(4, 2)


def _score_quad(quad: np.ndarray, hull: np.ndarray, area_total: float) -> float | None:
    """ให้คะแนนว่า quad นี้ "หน้าตาเหมือนใบฝากรถ" แค่ไหน คืน None ถ้าไม่ผ่านเกณฑ์

    เกณฑ์มาจากรูปร่างของใบจริง ไม่ใช่จากสีพื้นหลัง จึงใช้ได้กับทุกสถานที่ถ่าย
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

    # ยิ่งสัดส่วนใกล้ใบจริง และยิ่งเป็นสี่เหลี่ยมเต็ม ๆ ยิ่งได้คะแนนสูง
    ar_fit = 1.0 / (1.0 + abs(np.log(ar / SLIP_AR)) * 3)
    return float(ar_fit * rectangularity)


def _quad_candidates(
    mask: np.ndarray, shape: tuple[int, int], *, grow: float = 0.0
) -> list[tuple[float, np.ndarray]]:
    """ทุกก้อนใน mask ที่ผ่านเกณฑ์รูปร่าง พร้อมคะแนน

    ใช้ convex hull ของก้อนเป็นตัวตัดสิน เพราะกระดาษเป็นรูปนูน ส่วนรอยหยัก/รูโหว่
    ที่เกิดจากช่องว่างระหว่างตัวหนังสือไม่ควรถูกนับเป็นความไม่เป็นสี่เหลี่ยม
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
        quad = _quad_from_contour(hull)
        score = _score_quad(quad, hull, area_total)
        if score is not None:
            out.append((score, _expand_quad(quad, grow, shape)))
    return out


def find_paper_quad(bgr: np.ndarray) -> np.ndarray | None:
    """หา 4 มุมของกระดาษ คืน None ถ้าไม่มั่นใจ (ให้ caller fallback ไปใช้ภาพเต็ม)

    ลองทั้ง mask จากสีและ mask จากลวดลาย แล้วเลือกอันที่หน้าตาเหมือนใบฝากรถที่สุด
    """
    shape = bgr.shape[:2]
    candidates = (
        _quad_candidates(paper_mask(bgr), shape)
        + _quad_candidates(texture_mask(bgr), shape, grow=TEXTURE_GROW)
    )
    if not candidates:
        return None

    return _order_quad(max(candidates, key=lambda t: t[0])[1])


def warp_quad(bgr: np.ndarray, quad: np.ndarray, pad: int = 12) -> np.ndarray:
    """perspective warp ให้กระดาษเป็นสี่เหลี่ยมตรง (เผื่อขอบไว้เล็กน้อยกันตัวหนังสือโดนตัด)"""
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
    """หมุนให้ด้านยาวเป็นแนวนอน (สลิปเป็นแนวนอน) เหลือความกำกวมแค่ 0 vs 180 องศา"""
    h, w = bgr.shape[:2]
    return cv2.rotate(bgr, cv2.ROTATE_90_CLOCKWISE) if h > w else bgr


def enhance(bgr: np.ndarray) -> np.ndarray:
    """ลบเงา + เพิ่ม contrast แบบนุ่ม ๆ ไม่ binarize เพราะปากกาน้ำเงินจะหาย"""
    # ลบเงา/แสงไม่สม่ำเสมอ ด้วยการหารด้วย background ที่ได้จาก median blur
    bg = cv2.medianBlur(bgr, 31)
    flat = cv2.divide(bgr, bg, scale=192)

    lab = cv2.cvtColor(flat, cv2.COLOR_BGR2LAB)
    l, a, b = cv2.split(lab)
    l = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8)).apply(l)
    out = cv2.cvtColor(cv2.merge((l, a, b)), cv2.COLOR_LAB2BGR)

    # unsharp เบา ๆ ให้เส้นปากกาคมขึ้น
    blur = cv2.GaussianBlur(out, (0, 0), 2.0)
    return cv2.addWeighted(out, 1.5, blur, -0.5, 0)


def preprocess(source: str | Path | bytes) -> PreprocessResult:
    raw_pil = load_image(source)
    bgr = to_bgr(raw_pil)

    quad = find_paper_quad(bgr)
    # หมุนให้เป็นแนวนอนเฉพาะตอน crop สำเร็จ เพราะรู้แน่ว่าภาพที่ได้คือตัวใบ
    # ถ้าหาไม่เจอแล้วไปหมุนทั้งเฟรม ใบที่ถ่ายมาตรง ๆ จะกลายเป็นตะแคงแทน
    cropped = landscape(warp_quad(bgr, quad)) if quad is not None else bgr

    return PreprocessResult(
        raw=raw_pil,
        cropped=to_pil(cropped),
        enhanced=to_pil(enhance(cropped)),
        quad_found=quad is not None,
    )


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="preprocess ใบฝากรถ แล้วดูผลเป็นไฟล์ JPEG")
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
