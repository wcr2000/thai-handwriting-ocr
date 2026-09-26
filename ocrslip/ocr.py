"""เรียก OpenRouter ให้อ่านใบฝากรถออกมาเป็น JSON ตาม SLIP_JSON_SCHEMA"""

from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from .config import OPENROUTER_API_KEY, OPENROUTER_BASE_URL
from .schema import PROMPT, SLIP_JSON_SCHEMA, canonicalize


@dataclass
class OcrResult:
    model: str
    fields: dict[str, Any]              # ค่าที่อ่านได้ (name, tel, ...)
    confidence: dict[str, float]
    latency_s: float
    usage: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    raw_text: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def _data_url(jpeg: bytes) -> str:
    return "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()


def _extract_json(text: str) -> dict:
    """model บางตัวห่อ JSON ด้วย markdown fence หรือมีคำอธิบายนำหน้า"""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError(f"ไม่พบ JSON ใน response: {text[:200]}")
    return json.loads(text[start : end + 1])


def _payload(model: str, jpeg: bytes, structured: bool) -> dict:
    body: dict[str, Any] = {
        "model": model,
        "temperature": 0,
        # model รุ่นใหม่คิดก่อนตอบ (reasoning tokens) ถ้า budget น้อย JSON จะถูกตัดกลางคัน
        "max_tokens": 4000,
        "reasoning": {"effort": "low"},
        "usage": {"include": True},
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": PROMPT},
                    {"type": "image_url", "image_url": {"url": _data_url(jpeg)}},
                ],
            }
        ],
    }
    if structured:
        body["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "slip", "strict": True, "schema": SLIP_JSON_SCHEMA},
        }
    return body


def read_slip(
    jpeg: bytes,
    model: str,
    client: httpx.Client | None = None,
    retries: int = 3,
) -> OcrResult:
    """ยิงรูป 1 ใบเข้า model 1 ตัว คืนค่าที่อ่านได้ (ไม่ raise — error เก็บไว้ในผลลัพธ์)"""
    own_client = client is None
    client = client or httpx.Client(timeout=180)
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "X-Title": "ocr-dhammakaya",
    }

    structured = True
    last_error = "unknown"
    started = time.monotonic()
    try:
        for attempt in range(retries):
            try:
                r = client.post(
                    f"{OPENROUTER_BASE_URL}/chat/completions",
                    headers=headers,
                    json=_payload(model, jpeg, structured),
                )
                if r.status_code == 400 and structured:
                    # model ไม่รองรับ json_schema — ถอยไปใช้ prompt ล้วนแล้วแกะ JSON เอง
                    structured = False
                    continue
                if r.status_code in (429, 500, 502, 503, 504):
                    last_error = f"HTTP {r.status_code}: {r.text[:200]}"
                    time.sleep(2 ** attempt)
                    continue
                r.raise_for_status()
                body = r.json()

                if "error" in body and not body.get("choices"):
                    last_error = str(body["error"])[:300]
                    time.sleep(2 ** attempt)
                    continue

                text = body["choices"][0]["message"]["content"]
                if isinstance(text, list):  # บาง provider คืน content เป็น list ของ block
                    text = "".join(b.get("text", "") for b in text)
                parsed = _extract_json(text)
                conf = parsed.pop("confidence", None) or {}
                return OcrResult(
                    model=model,
                    fields=canonicalize(parsed),
                    confidence={k: float(v) for k, v in conf.items() if isinstance(v, (int, float))},
                    latency_s=round(time.monotonic() - started, 2),
                    usage=body.get("usage", {}),
                    raw_text=text,
                )
            except (httpx.HTTPError, ValueError, KeyError, json.JSONDecodeError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"[:300]
                time.sleep(2 ** attempt)

        return OcrResult(
            model=model, fields={}, confidence={},
            latency_s=round(time.monotonic() - started, 2), error=last_error,
        )
    finally:
        if own_client:
            client.close()
