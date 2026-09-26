"""Preprocessing ใบฝากรถ: จับมุมกระดาษ -> warp -> หมุนให้ด้านยาวเป็นแนวนอน -> ปรับสี

ภาพต้นทางเป็นรูปถ่ายมือถือ: สลิปแผ่นเล็กวางบนโต๊ะไม้ มักถ่ายตะแคง 90/180 องศา
เขียนด้วยปากกาน้ำเงินจาง จึงห้าม binarize แรงเพราะเส้นปากกาจะหาย
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


def paper_mask(bgr: np.ndarray) -> np.ndarray:
    """mask ของกระดาษ: สว่าง + แทบไม่มีสี (โต๊ะไม้เป็นสีส้มอิ่มสี จึงถูกตัดออก)"""
    hsv = cv2.cvtColor(cv2.GaussianBlur(bgr, (7, 7), 0), cv2.COLOR_BGR2HSV)
    s, v = hsv[:, :, 1], hsv[:, :, 2]
    # ใช้ Otsu หา threshold เองจากการกระจายของภาพ แทนการ hardcode ค่าคงที่
    v_thr, _ = cv2.threshold(v, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    s_thr, _ = cv2.threshold(s, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    mask = ((v >= max(v_thr, 110)) & (s <= max(s_thr * 0.9, 60))).astype(np.uint8) * 255
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((15, 15), np.uint8))
    return cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((9, 9), np.uint8))


def find_paper_quad(bgr: np.ndarray) -> np.ndarray | None:
    """หา 4 มุมของกระดาษ คืน None ถ้าไม่มั่นใจ (ให้ caller fallback ไปใช้ภาพเต็ม)"""
    h, w = bgr.shape[:2]
    area_total = h * w

    mask = paper_mask(bgr)
    n, labels, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    best_idx, best_area = None, 0
    for i in range(1, n):
        area = stats[i, cv2.CC_STAT_AREA]
        if area > best_area and 0.03 * area_total < area < 0.95 * area_total:
            best_idx, best_area = i, area
    if best_idx is None:
        return None

    blob = (labels == best_idx).astype(np.uint8) * 255
    contours, _ = cv2.findContours(blob, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    c = max(contours, key=cv2.contourArea)

    # ลอง approx เป็น 4 เหลี่ยมก่อน (ได้ perspective ที่แม่นกว่า) ถ้าไม่ได้ค่อยใช้ minAreaRect
    peri = cv2.arcLength(c, True)
    quad = None
    for eps in (0.02, 0.03, 0.04, 0.06):
        approx = cv2.approxPolyDP(c, eps * peri, True)
        if len(approx) == 4 and cv2.isContourConvex(approx):
            quad = approx.astype(np.float32)
            break
    if quad is None:
        quad = cv2.boxPoints(cv2.minAreaRect(c)).astype(np.float32)

    # กันกรณี approx เบี้ยวจนกินพื้นที่นอกกระดาษ: ถ้าใหญ่กว่า contour จริงมาก ใช้ minAreaRect แทน
    if cv2.contourArea(quad) > 1.25 * cv2.contourArea(c):
        quad = cv2.boxPoints(cv2.minAreaRect(c)).astype(np.float32)

    return _order_quad(quad)


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
    cropped = landscape(warp_quad(bgr, quad) if quad is not None else bgr)

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
