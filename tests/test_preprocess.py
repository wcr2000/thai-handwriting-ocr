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
import ocrslip.preprocess as P
from ocrslip.preprocess import find_paper_quad, preprocess

FRAME = (2000, 1500)          # (สูง, กว้าง) เลียนแบบรูปถ่ายมือถือแนวตั้ง
SLIP = (540, 1150)            # (สูง, กว้าง) ของใบ สัดส่วน ~2.1 และกินพื้นที่ ~21% ของเฟรม เท่าของจริง

WOOD = (40, 110, 190)         # BGR โต๊ะไม้สีส้ม — พื้นหลังแบบที่โค้ดเดิมรองรับ
WHITE_DESK = (205, 207, 208)  # BGR โต๊ะ/ผนังขาวเทา — พื้นหลังที่ทำให้โค้ดเดิมพัง


def fake_photo(bg: tuple[int, int, int], angle: float = 0.0) -> np.ndarray:
    """รูปถ่ายจำลอง: ใบสีขาวมีเส้นพิมพ์ + ลายมือ วางเอียงบนพื้นหลังสีที่กำหนด"""
    img = np.full((*FRAME, 3), bg, np.uint8)

    slip = np.full((*SLIP, 3), 250, np.uint8)
    for i in range(1, 5):                       # เส้นบรรทัดพิมพ์
        y = i * SLIP[0] // 5
        cv2.line(slip, (60, y), (SLIP[1] - 60, y), (120, 120, 120), 3)
    for i in range(1, 5):                       # ลายมือปากกาน้ำเงิน
        y = i * SLIP[0] // 5
        cv2.putText(slip, "0812345678", (120, y - 14), 0, 1.6, (150, 60, 30), 4)

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
    assert 0.1 < coverage < 0.45, f"crop กินพื้นที่ {coverage:.0%} ของเฟรม — ไม่ใช่ขนาดของตัวใบ"


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


def test_blank_paper_nearby_does_not_beat_the_slip():
    """ปึกกระดาษเปล่าข้าง ๆ ที่บังเอิญมีสัดส่วนใกล้ใบ ต้องไม่ชนะตัวใบจริง

    regression: เคยมีมุมของปึกกระดาษเปล่าขนาด 3% ของเฟรม ได้คะแนนสัดส่วนดีกว่าตัวใบ
    ที่วางเอียงนิดหน่อย ระบบเลย crop ไปโดนกระดาษเปล่า แล้ว OCR อ่านได้ null ทุกช่อง
    """
    img = fake_photo(WOOD, angle=6.0)
    # กระดาษเปล่าสัดส่วน 2:1 วางมุมล่างซ้าย เล็กกว่าใบจริงมาก
    cv2.rectangle(img, (60, FRAME[0] - 380), (60 + 360, FRAME[0] - 200), (248, 248, 248), -1)

    r = run(img)
    assert r.quad_found
    w, h = r.cropped.size
    coverage = (w * h) / (FRAME[0] * FRAME[1])
    assert coverage > 0.1, f"crop ได้แค่ {coverage:.1%} ของเฟรม — ไปจับกระดาษเปล่าแทนตัวใบ"


@pytest.mark.parametrize("orientation,rotated", [
    ("upside_down", True), ("upright", False), ("", False),
])
def test_upright_rotates_only_when_model_says_upside_down(orientation, rotated):
    """landscape() แก้ได้แค่ 90 องศา ส่วน 0 vs 180 ต้องเชื่อสิ่งที่ model อ่านได้"""
    img = to_pil(fake_photo(WOOD))
    out = P.upright(img, orientation)
    assert (out is not img) == rotated
    if rotated:
        assert np.array_equal(np.asarray(out), np.asarray(img)[::-1, ::-1])


def test_raw_ocr_always_records_orientation():
    """raw_ocr ต้องมี orientation เสมอ

    reprocess ใช้ฟิลด์นี้ตัดสินว่าใบไหน "ยังไม่เคยเช็คว่ากลับหัว" ถ้าไม่บันทึกไว้
    ใบที่เพิ่งอัปโหลดจะถูกหยิบไปยิง model ซ้ำทุกครั้งที่รัน reprocess
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
    """เรียงมุมต้องไม่ทำให้เหลือ 3 มุม

    regression: วิธีเดิมเรียงตาม min/max ของ x+y และ x-y ซึ่งเลือกจุดเดิมซ้ำได้เมื่อ
    สี่เหลี่ยมเอียงมาก quad ที่ได้จึงเสียรูปแล้ว warp ออกมาเป็นภาพเบลอไม่มีอะไรเลย
    """
    for angle in range(0, 90, 7):
        rect = cv2.boxPoints(((500.0, 400.0), (600.0, 280.0), float(angle)))
        ordered = P._order_quad(rect.astype(np.float32))
        assert len({tuple(np.round(p, 3)) for p in ordered}) == 4, f"มุมซ้ำที่ {angle} องศา"

        tl, tr, br, bl = ordered
        assert tl[0] < tr[0] or tl[1] < bl[1]          # เรียงตามเข็ม เริ่มจากซ้ายบน
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
    # crop ดี อ่านได้ตั้งแต่ครั้งแรก — ห้ามยิงซ้ำ
    (True, [_ocr(name="ก", tel="0812345678", noplate="กก1234")], False, 1),
    # อ่านออก แต่ model บอกว่าตัวใบไม่ได้กินพื้นที่เกือบทั้งภาพ = crop ไปโดนพื้นโต๊ะ
    # ต้องลองภาพเต็ม แม้จะอ่านได้ครบแล้วก็ตาม
    (True, [_ocr(fills_frame=False, name="ก", tel="0812345678"),
            _ocr(name="ข", tel="0899999999")], True, 2),
    # crop พัง อ่านไม่ได้เลย แล้วภาพเต็มอ่านได้ — ต้องใช้ผลจากภาพเต็ม
    (True, [_ocr(), _ocr(name="ก", tel="0812345678")], True, 2),
    # หาขอบไม่เจอตั้งแต่แรก ภาพที่ส่งไปคือภาพเต็มอยู่แล้ว — ยิงซ้ำไปก็ได้ผลเดิม
    (False, [_ocr()], False, 1),
    # ลองภาพเต็มแล้วก็ยังอ่านไม่ได้ — คืนผลแรกไป ไม่ใช่ทำให้แย่ลง
    (True, [_ocr(), _ocr()], False, 2),
])
def test_read_with_fallback_retries_full_frame_only_when_crop_reads_nothing(
    monkeypatch, quad_found, reads, want_full, want_calls
):
    """ตาข่ายกันตกเวลา crop ไปจับของผิด

    มีสองสัญญาณ: อ่านไม่ได้สักช่อง (ไปจับของที่ไม่มีตัวหนังสือ) และ model บอกว่า
    ตัวใบไม่ได้กินพื้นที่เกือบทั้งภาพ (จับติดพื้นหลังมาเยอะจนตัวใบเล็กและเอียง)
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
