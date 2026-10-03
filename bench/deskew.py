"""Experiment: does deskewing after the crop improve reading accuracy?

The answer is **no** — see bench/crop_report.md.
Close-up shots improve marginally (CER 0.122 -> 0.120, within the noise), but distant shots
get clearly worse (0.132 -> 0.161), because rotating means interpolating afresh and text that
was already small blurs further. The model itself tolerates this much skew anyway.

The code is kept as evidence that this was tried, so nobody tries it again.
"""

from __future__ import annotations

import cv2
import numpy as np

from ocrslip.preprocess import ink_mask

MAX_DESKEW = 25   # the largest angle still counted as "a tilted text line" rather than a vertical rule


def text_skew(bgr: np.ndarray) -> float:
    """The tilt of the slip's text lines, in degrees — 0 when it cannot be measured.

    Measured from the long horizontal lines, which on a slip are the printed rules and the run
    of the handwriting. The median is used so one stray line cannot drag the result.
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
    """Rotate the cropped image so its text lines run horizontally"""
    angle = text_skew(bgr)
    if abs(angle) < 1.0:
        return bgr
    h, w = bgr.shape[:2]
    m = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
    return cv2.warpAffine(bgr, m, (w, h), flags=cv2.INTER_CUBIC,
                          borderMode=cv2.BORDER_REPLICATE)
