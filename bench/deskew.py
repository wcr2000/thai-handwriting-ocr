"""ทดลอง: หมุนแก้เอียงหลัง crop ช่วยให้อ่านแม่นขึ้นไหม

ผลคือ **ไม่ช่วย** — ดู bench/crop_report.md
ใบถ่ายใกล้ดีขึ้นนิดเดียว (CER 0.122 -> 0.120 ซึ่งอยู่ในช่วง noise)
แต่ใบถ่ายไกลแย่ลงชัดเจน (0.132 -> 0.161) เพราะการหมุนต้อง interpolate ใหม่
แล้วตัวหนังสือที่เล็กอยู่แล้วยิ่งเบลอ ส่วนตัว model เองทนความเอียงระดับนี้ได้อยู่แล้ว

เก็บโค้ดไว้เป็นหลักฐานว่าลองแล้ว จะได้ไม่มีใครมาลองซ้ำ
"""

from __future__ import annotations

import cv2
import numpy as np

from ocrslip.preprocess import ink_mask

MAX_DESKEW = 25   # องศาสูงสุดที่ยอมนับว่าเป็น "เส้นบรรทัดที่เอียง" ไม่ใช่เส้นแนวตั้ง


def text_skew(bgr: np.ndarray) -> float:
    """มุมเอียงของเส้นบรรทัดในใบ (องศา) — 0 ถ้าวัดไม่ได้

    วัดจากเส้นยาวแนวนอน ซึ่งในใบคือเส้นบรรทัดพิมพ์กับแนวตัวหนังสือ
    ใช้ค่ามัธยฐานเพื่อไม่ให้เส้นแปลกปลอมเส้นเดียวลากผลลัพธ์
    """
    ink = ink_mask(bgr) * 255
    w = bgr.shape[1]
    lines = cv2.HoughLinesP(
        ink, 1, np.pi / 360, threshold=80,
        minLineLength=int(w * 0.35), maxLineGap=int(w * 0.02),
    )
    if lines is None:
        return 0.0
    angles = [
        a for x1, y1, x2, y2 in lines.reshape(-1, 4)
        if abs(a := float(np.degrees(np.arctan2(y2 - y1, x2 - x1)))) < MAX_DESKEW
    ]
    return float(np.median(angles)) if len(angles) >= 3 else 0.0


def deskew(bgr: np.ndarray) -> np.ndarray:
    """หมุนภาพที่ crop แล้วให้เส้นบรรทัดอยู่ในแนวนอน"""
    angle = text_skew(bgr)
    if abs(angle) < 1.0:
        return bgr
    h, w = bgr.shape[:2]
    m = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
    return cv2.warpAffine(bgr, m, (w, h), flags=cv2.INTER_CUBIC,
                          borderMode=cv2.BORDER_REPLICATE)
