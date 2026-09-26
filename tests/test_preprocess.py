"""ทดสอบการจับขอบกระดาษ — regression ของบั๊กที่ crop ไปโดนพื้นหลังแทนใบฝากรถ

บั๊กเดิม: paper_mask หากระดาษจาก "สว่าง + ไม่มีสี" ซึ่งใช้ได้เฉพาะตอนวางบนโต๊ะไม้สีส้ม
พอถ่ายบนโต๊ะ/ผนังสีขาว พื้นหลังเข้าเกณฑ์เดียวกับกระดาษ ระบบจึง crop ทั้งเฟรม
แล้ว landscape() หมุน 90 องศาให้อีก โดยที่ quad_found ยังรายงานว่า true

ภาพในไฟล์นี้สร้างขึ้นเองทั้งหมด ห้ามใช้ใบฝากรถจริงมาเป็น fixture เพราะ repo นี้เปิดสาธารณะ
"""

import cv2
import numpy as np
import pytest

from ocrslip.imageio import encode_jpeg, to_pil
from ocrslip.preprocess import find_paper_quad, preprocess

FRAME = (2000, 1500)          # (สูง, กว้าง) เลียนแบบรูปถ่ายมือถือแนวตั้ง
SLIP = (240, 520)             # (สูง, กว้าง) ของใบ อัตราส่วน ~2.17 เท่าของจริง

WOOD = (40, 110, 190)         # BGR โต๊ะไม้สีส้ม — พื้นหลังแบบที่โค้ดเดิมรองรับ
WHITE_DESK = (205, 207, 208)  # BGR โต๊ะ/ผนังขาวเทา — พื้นหลังที่ทำให้โค้ดเดิมพัง


def fake_photo(bg: tuple[int, int, int], angle: float = 0.0) -> np.ndarray:
    """รูปถ่ายจำลอง: ใบสีขาวมีเส้นพิมพ์ + ลายมือ วางเอียงบนพื้นหลังสีที่กำหนด"""
    img = np.full((*FRAME, 3), bg, np.uint8)

    slip = np.full((*SLIP, 3), 250, np.uint8)
    for i in range(1, 5):                       # เส้นบรรทัดพิมพ์
        y = i * SLIP[0] // 5
        cv2.line(slip, (30, y), (SLIP[1] - 30, y), (120, 120, 120), 2)
    for i in range(1, 5):                       # ลายมือปากกาน้ำเงิน
        y = i * SLIP[0] // 5
        cv2.putText(slip, "0812345678", (60, y - 6), 0, 0.8, (150, 60, 30), 2)

    return _paste(img, slip, angle)


def _paste(img: np.ndarray, slip: np.ndarray, angle: float) -> np.ndarray:
    """วาง slip ลงกลางภาพโดยหมุน angle องศา"""
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


@pytest.mark.parametrize("bg,label", [(WOOD, "โต๊ะไม้"), (WHITE_DESK, "โต๊ะขาว")])
@pytest.mark.parametrize("angle", [0.0, 8.0, -12.0])
def test_crop_finds_slip_on_any_background(bg, label, angle):
    """ต้อง crop ได้เฉพาะตัวใบ ไม่ว่าพื้นหลังจะเป็นสีอะไร — นี่คือ regression ของบั๊กเดิม"""
    r = run(fake_photo(bg, angle))
    assert r.quad_found, f"หาขอบกระดาษไม่เจอบน{label} ที่ {angle} องศา"

    w, h = r.cropped.size
    assert w > h, "ผลลัพธ์ต้องเป็นแนวนอนเสมอ"

    ar = w / h
    assert 1.6 <= ar <= 3.0, f"สัดส่วนเพี้ยน ({ar:.2f}) แปลว่า crop ไม่ได้ลงบนตัวใบ"

    coverage = (w * h) / (FRAME[0] * FRAME[1])
    assert coverage < 0.35, f"crop กินพื้นที่ {coverage:.0%} ของเฟรม — แปลว่าไปจับพื้นหลังแทนใบ"


def test_plain_background_is_not_mistaken_for_paper():
    """พื้นหลังขาวล้วนไม่มีใบ ต้องรายงานว่าหาไม่เจอ แล้วคืนภาพเต็มแทนการ crop มั่ว

    ยอมให้ OCR อ่านภาพเต็มดีกว่าปล่อยภาพที่ถูกบิด/หมุน/ตัดขอบไปโดยไม่มีใครรู้
    """
    img = np.full((*FRAME, 3), WHITE_DESK, np.uint8)
    assert find_paper_quad(img) is None

    r = run(img)
    assert not r.quad_found
    assert r.cropped.size == r.raw.size


def test_giant_bright_blob_is_rejected():
    """ก้อนสว่างที่ใหญ่เกือบเต็มเฟรม (ผนัง/โต๊ะ) ต้องไม่ถูกนับเป็นกระดาษ"""
    img = np.full((*FRAME, 3), (60, 60, 60), np.uint8)
    cv2.rectangle(img, (20, 20), (FRAME[1] - 20, FRAME[0] - 20), (230, 232, 233), -1)
    assert find_paper_quad(img) is None
