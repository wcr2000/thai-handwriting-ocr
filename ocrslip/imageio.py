"""อ่านไฟล์ภาพจากทุกนามสกุล โดยดูจาก magic bytes ไม่เชื่อนามสกุลไฟล์

รูปตัวอย่างในโปรเจกต์นี้เป็น HEIC ทั้งหมด แม้บางไฟล์จะตั้งชื่อเป็น .png / .jpg
(ดูเหมือนถูก rename มาตอนส่งไฟล์) การเชื่อนามสกุลจะทำให้ decode พัง
"""

from __future__ import annotations

import io
from pathlib import Path

import numpy as np
import pillow_heif
from PIL import Image, ImageOps

pillow_heif.register_heif_opener()

MAX_LONG_SIDE = 2000


def sniff_format(data: bytes) -> str:
    """เดาชนิดไฟล์จาก magic bytes -> 'heic' | 'jpeg' | 'png' | 'webp' | 'unknown'"""
    if data[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[4:8] == b"ftyp":
        brand = data[8:12]
        if brand in (b"heic", b"heix", b"hevc", b"heim", b"heis", b"hevm", b"mif1", b"msf1"):
            return "heic"
        if brand in (b"avif", b"avis"):
            return "avif"
    return "unknown"


def load_image(source: str | Path | bytes) -> Image.Image:
    """อ่านเป็น PIL RGB พร้อม apply EXIF orientation และย่อด้านยาวไม่เกิน MAX_LONG_SIDE"""
    data = Path(source).read_bytes() if isinstance(source, (str, Path)) else source
    fmt = sniff_format(data)
    if fmt == "unknown":
        # ปล่อยให้ Pillow ลองเอง เผื่อเป็นฟอร์แมตที่เรายังไม่รู้จัก
        pass
    img = Image.open(io.BytesIO(data))
    img = ImageOps.exif_transpose(img)
    img = img.convert("RGB")
    if max(img.size) > MAX_LONG_SIDE:
        scale = MAX_LONG_SIDE / max(img.size)
        img = img.resize((round(img.width * scale), round(img.height * scale)), Image.LANCZOS)
    return img


def to_bgr(img: Image.Image) -> np.ndarray:
    """PIL RGB -> OpenCV BGR"""
    return np.asarray(img)[:, :, ::-1].copy()


def to_pil(bgr: np.ndarray) -> Image.Image:
    """OpenCV BGR -> PIL RGB"""
    return Image.fromarray(bgr[:, :, ::-1])


def encode_jpeg(img: Image.Image, quality: int = 88) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=True)
    return buf.getvalue()
