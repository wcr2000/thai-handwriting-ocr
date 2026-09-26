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
    """เรียงจุด 4 มุมเป็น [top-left, top-right, bottom-right, bottom-left]

    เรียงตามมุมรอบจุดกึ่งกลาง ไม่ใช่ตาม min/max ของผลบวก/ผลต่างพิกัด — วิธีนั้นเลือก
    จุดเดิมซ้ำได้เมื่อสี่เหลี่ยมเอียงมาก ทำให้ quad เหลือ 3 มุมแล้ว warp ออกมาเป็นภาพเละ
    """
    pts = pts.reshape(4, 2).astype(np.float32)
    center = pts.mean(axis=0)
    # แกน y ชี้ลง การเรียงตามมุมที่เพิ่มขึ้นจึงได้ลำดับตามเข็มนาฬิกา
    pts = pts[np.argsort(np.arctan2(pts[:, 1] - center[1], pts[:, 0] - center[0]))]
    return np.roll(pts, -int(np.argmin(pts.sum(axis=1))), axis=0)


# รูปร่างของใบฝากรถจริง: อัตราส่วนด้านยาว/ด้านสั้นราว 2.1
# ส่วน "ขนาด" ใช้เป็นเกณฑ์ไม่ได้ เพราะแต่ละคนถ่ายห่างไม่เท่ากัน — วัดจากของจริงได้ตั้งแต่
# กินพื้นที่ 5% ไปจนถึง 65% ของเฟรม จึงเปิดช่วงกว้างแล้วไปตัดสินด้วยหลักฐานอื่นแทน
SLIP_AR = 2.1
AR_RANGE = (1.45, 3.4)
AREA_RANGE = (0.02, 0.70)
MIN_RECTANGULARITY = 0.7    # convex hull ต้องเต็ม minAreaRect เกินเท่านี้ ไม่งั้นไม่ใช่กระดาษสี่เหลี่ยม

# สัดส่วนพิกเซลที่เป็นหมึกขั้นต่ำในบริเวณที่จะ crop — ใบที่กรอกแล้วมี 0.040-0.108
# ส่วนของที่ไม่ใช่ใบ (ผ้า/พื้นเรียบ) มี 0.002 จึงตั้งไว้ต่ำ ๆ แค่กันของที่ "ว่างเปล่าชัดเจน"
MIN_INK = 0.02

# จำนวนชิ้นหมึกขนาด "ตัวอักษร" ขั้นต่ำ — หลักฐานที่ไม่ขึ้นกับว่าถ่ายใกล้หรือไกล
# ใบที่กรอกแล้วนับได้หลักร้อย ส่วนมุมปึกกระดาษเปล่านับได้ 8 (เป็นแค่เส้นขอบระหว่างแผ่น)
MIN_CHARS = 40
CHAR_HALF = 150   # จำนวนชิ้นที่ให้คะแนนครึ่งหนึ่ง — ใบเต็มใบได้ 400-700 เศษไม้ได้ราวร้อยเดียว

# ขยาย quad ออกจากจุดกึ่งกลางก่อน warp — กันตัวหนังสือริมขอบโดนตัด
# กินพื้นหลังเข้ามานิดหน่อยไม่เป็นไร แต่ตัดตัวหนังสือหายคือข้อมูลหาย
PAPER_GROW = 0.06     # mask จากสี: ได้ขอบกระดาษอยู่แล้ว เผื่อเฉพาะส่วนที่เงาบังจน mask กินไม่ถึงขอบ
TEXTURE_GROW = 0.14   # mask จากลวดลาย: จับได้แค่บริเวณหมึกซึ่งอยู่ในกรอบพิมพ์ ต้องเผื่อมากกว่า


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


def ink_mask(bgr: np.ndarray) -> np.ndarray:
    """พิกเซลที่เข้มกว่าพื้นรอบ ๆ อย่างชัดเจน = เส้นพิมพ์ + ลายมือ

    เทียบกับ background ที่ได้จาก median blur แทนค่าคงที่ จะได้ไม่แพ้เงาหรือกระดาษสีครีม
    """
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY).astype(np.int16)
    bg = cv2.medianBlur(cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY), 31).astype(np.int16)
    return ((bg - gray) > 28).astype(np.uint8)


def texture_mask(bgr: np.ndarray, close_px: float) -> np.ndarray:
    """mask จากลวดลาย: ในใบมีเส้นประพิมพ์ + ลายมือ ส่วนโต๊ะ/ผนังเรียบ

    ใช้ได้แม้พื้นหลังจะขาวพอ ๆ กับกระดาษ เพราะแยกด้วย "ความไม่เรียบ" ไม่ใช่ความสว่าง
    close_px คือระยะที่ยอมเชื่อมรอยหมึกที่อยู่ห่างกันให้เป็นก้อนเดียว
    """
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    # ความต่างของ local max/min ในหน้าต่างเล็ก ๆ — สูงตรงที่มีเส้น ต่ำตรงพื้นเรียบ
    k = np.ones((9, 9), np.uint8)
    detail = cv2.subtract(cv2.dilate(gray, k), cv2.erode(gray, k))
    thr, _ = cv2.threshold(detail, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    mask = (detail >= max(thr, 24)).astype(np.uint8) * 255

    # เส้นในใบอยู่ห่างกัน ต้องเชื่อมให้เป็นก้อนเดียวก่อน แล้วค่อยลบจุดรบกวนเล็ก ๆ
    close = max(int(close_px) | 1, 9)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((close, close), np.uint8))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((9, 9), np.uint8))
    return mask


def texture_masks(bgr: np.ndarray) -> list[np.ndarray]:
    """texture_mask หลายสเกล เพราะไม่รู้ล่วงหน้าว่าคนถ่ายห่างแค่ไหน

    ระยะเชื่อมที่พอดีกับใบที่ถ่ายเต็มเฟรม จะใหญ่เกินไปสำหรับใบที่ถ่ายไกล
    จนเชื่อมตัวใบติดกับลายไม้รอบ ๆ กลายเป็นก้อนเดียว แล้วรูปร่างก็เพี้ยนจนตกเกณฑ์
    """
    long_side = max(bgr.shape[:2])
    return [texture_mask(bgr, long_side * f) for f in (0.015, 0.03, 0.05)]


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


def _score_quad(
    quad: np.ndarray, hull: np.ndarray, area_total: float, chars: int = 0
) -> float | None:
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

    # ยิ่งสัดส่วนใกล้ใบจริง เป็นสี่เหลี่ยมเต็ม ๆ และมีตัวหนังสืออยู่ข้างในเยอะ ยิ่งได้คะแนนสูง
    #
    # จำนวนตัวหนังสือสำคัญกว่าที่คิด: เศษพื้นไม้ชิ้นเล็ก ๆ ที่สัดส่วน 2:1 พอดีเคยชนะตัวใบจริง
    # ทั้งที่นับชิ้นหมึกได้ 116 ส่วนตัวใบได้ 441 — รูปร่างอย่างเดียวแยกสองอย่างนี้ไม่ออก
    ar_fit = 1.0 / (1.0 + abs(np.log(ar / SLIP_AR)) * 3)
    char_fit = chars / (chars + CHAR_HALF)
    return float(ar_fit * rectangularity * char_fit)


def ink_evidence(quad: np.ndarray, ink: np.ndarray) -> tuple[float, int]:
    """หลักฐานว่าใน quad นี้มี "ข้อความที่กรอกไว้" จริง คืน (สัดส่วนหมึก, จำนวนชิ้นขนาดตัวอักษร)

    จำนวนชิ้นขนาดตัวอักษรเป็นหลักฐานที่ไม่ขึ้นกับระยะถ่าย เพราะนับเทียบกับขนาดของ
    quad เอง ไม่ใช่ขนาดภาพ — ใบเดียวกันถ่ายใกล้หรือไกลก็ได้จำนวนใกล้เคียงกัน
    """
    region = np.zeros(ink.shape, np.uint8)
    cv2.fillConvexPoly(region, quad.astype(np.int32), 1)
    inside = ink * region
    area = max(int(region.sum()), 1)

    side = np.sqrt(area)
    n, _, stats, _ = cv2.connectedComponentsWithStats(inside, 8)
    chars = sum(
        1 for i in range(1, n)
        # ไม่เล็กจนเป็นจุดรบกวน และไม่ยาวจนเป็นเส้นบรรทัด/ขอบกระดาษ
        if 0.005 * side < max(stats[i, 2], stats[i, 3]) < 0.25 * side and stats[i, 4] > 4
    )
    return float(inside.sum() / area), chars


def _quad_candidates(
    mask: np.ndarray, shape: tuple[int, int], ink: np.ndarray, *, grow: float = 0.0,
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
        quad = _expand_quad(_quad_from_contour(hull), grow, shape)

        # ใบที่กรอกแล้วต้องมีตัวหนังสืออยู่ข้างใน — กันไปจับผ้า/พื้นเรียบ/กระดาษเปล่า
        # ที่บังเอิญได้รูปร่างเข้าเกณฑ์ ใช้แทนการจำกัดขนาด ซึ่งใช้ไม่ได้เพราะแต่ละคน
        # ถ่ายห่างไม่เท่ากัน
        ratio, chars = ink_evidence(quad, ink)
        if ratio < MIN_INK or chars < MIN_CHARS:
            continue

        score = _score_quad(quad, hull, area_total, chars)
        if score is not None:
            out.append((score, quad))
    return out


def find_paper_quad(bgr: np.ndarray) -> np.ndarray | None:
    """หา 4 มุมของกระดาษ คืน None ถ้าไม่มั่นใจ (ให้ caller fallback ไปใช้ภาพเต็ม)

    ลองทั้ง mask จากสีและ mask จากลวดลาย แล้วเลือกอันที่หน้าตาเหมือนใบฝากรถที่สุด
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


def upright(img: Image.Image, orientation: str) -> Image.Image:
    """หมุนรูป 180 องศาถ้า model รายงานว่าใบกลับหัว คืนรูปเดิมถ้าไม่ต้องหมุน

    landscape() แก้ได้แค่ 90 องศา เหลือความกำกวม 0 vs 180 ที่ดูจากรูปร่างกระดาษไม่ออก
    ต้องอ่านตัวหนังสือถึงจะรู้ — ซึ่ง model ทำอยู่แล้ว จึงให้มันบอกมาเลยแทนการเดาเอง
    """
    return img.transpose(Image.ROTATE_180) if orientation == "upside_down" else img


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
