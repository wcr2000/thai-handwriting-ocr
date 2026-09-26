"""หากรอบใบฝากรถด้วย LLM แทนการใช้ OpenCV — ไว้เทียบว่าคุ้มกับการยิงเพิ่ม 1 ครั้งไหม

สถาปัตยกรรมแบบเดียวกับงาน OCR บัตรประชาชนทั่วไป: จับกรอบก่อน แล้วค่อยอ่าน
ต่างกันแค่ตัวจับกรอบเป็น LLM ไม่ใช่ detector ที่เทรนเอง
"""

from __future__ import annotations

import base64
import json
import time

import cv2
import httpx
import numpy as np

from ocrslip.config import OPENROUTER_API_KEY, OPENROUTER_BASE_URL

DETECT_PROMPT = """ในภาพนี้มี "ใบฝากรถ" เป็นกระดาษสี่เหลี่ยมผืนผ้าแผ่นเล็กที่มีลายมือเขียนอยู่

หา 4 มุมของใบนั้น แล้วตอบเป็น JSON อย่างเดียว ไม่ต้องอธิบาย:
{"found": true, "corners": [[x,y],[x,y],[x,y],[x,y]]}

- เรียงมุมตามเข็มนาฬิกาเริ่มจากมุมซ้ายบนของตัวใบ (ตามการวางจริงของใบ ไม่ใช่ของภาพ)
- พิกัดเป็นจำนวนเต็ม 0-1000 เทียบกับความกว้าง/สูงของภาพ
- เอาขอบกระดาษ ไม่ใช่แค่กรอบตาราง และต้องคลุมตัวหนังสือที่เขียนนอกตารางด้วย
- ถ้าในภาพไม่มีใบฝากรถเลย ตอบ {"found": false, "corners": []}
- ถ้ามีหลายใบซ้อนกัน เอาใบที่อยู่บนสุด/เห็นชัดที่สุดใบเดียว"""

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["found", "corners"],
    "properties": {
        "found": {"type": "boolean"},
        "corners": {
            "type": "array",
            "items": {"type": "array", "items": {"type": "number"}},
        },
    },
}


def detect_quad(
    jpeg: bytes, size: tuple[int, int], model: str, client: httpx.Client | None = None
) -> tuple[np.ndarray | None, dict, float]:
    """คืน (quad พิกัดพิกเซล, usage, latency) — quad = None ถ้าหาไม่เจอหรือตอบไม่เข้ารูป"""
    own = client is None
    client = client or httpx.Client(timeout=180)
    started = time.monotonic()
    try:
        r = client.post(
            f"{OPENROUTER_BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}", "X-Title": "ocr-dhammakaya"},
            json={
                "model": model,
                "temperature": 0,
                "max_tokens": 2000,
                "reasoning": {"effort": "low"},
                "usage": {"include": True},
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {"name": "quad", "strict": True, "schema": SCHEMA},
                },
                "messages": [{"role": "user", "content": [
                    {"type": "text", "text": DETECT_PROMPT},
                    {"type": "image_url", "image_url": {"url":
                        "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()}},
                ]}],
            },
        )
        r.raise_for_status()
        body = r.json()
        text = body["choices"][0]["message"]["content"]
        if isinstance(text, list):
            text = "".join(b.get("text", "") for b in text)
        parsed = json.loads(text[text.find("{"): text.rfind("}") + 1])
        latency = time.monotonic() - started
        usage = body.get("usage", {})

        corners = parsed.get("corners") or []
        if not parsed.get("found") or len(corners) != 4:
            return None, usage, latency
        w, h = size
        quad = np.array([[c[0] / 1000.0 * w, c[1] / 1000.0 * h] for c in corners], dtype=np.float32)
        if cv2.contourArea(quad) < 0.005 * w * h:
            return None, usage, latency
        return quad, usage, latency
    except Exception:
        return None, {}, time.monotonic() - started
    finally:
        if own:
            client.close()
