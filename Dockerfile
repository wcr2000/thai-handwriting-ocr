# syntax=docker/dockerfile:1
# ─────────────────────────────────────────────────────────────────────────────
# stage 1: builder — ติดตั้ง dependency ลง venv แยก (ไม่เอา toolchain ติดไป image จริง)
# ─────────────────────────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# copy requirements.txt ก่อน source — layer นี้จะถูก cache ไว้จนกว่า dependency จะเปลี่ยน
COPY requirements.txt ./
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt

# ─────────────────────────────────────────────────────────────────────────────
# stage 2: runtime
# ─────────────────────────────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

# ทดสอบแล้วว่า wheel ที่ใช้ไม่ต้องพึ่ง OS lib เพิ่มเลย:
#   opencv-python-headless ไม่ต้องมี libgl1 (ไม่มี GUI backend) และ 5.x bundle libglib มาให้เอง
#   pillow-heif bundle libheif / psycopg[binary] bundle libpq มาในตัว wheel
# เลยลงแค่ curl ไว้ให้ HEALTHCHECK ใช้
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    PORT=8000

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
# เอาเฉพาะของที่ต้องใช้ตอนรัน: โค้ด + template + static + schema.sql (ใช้ตอน db init)
COPY ocrslip ./ocrslip
COPY db ./db

# รันด้วย non-root
RUN useradd --create-home --uid 10001 ocrslip && chown -R ocrslip:ocrslip /app
USER ocrslip

EXPOSE 8000

# /static/app.css ตอบ 200 ได้โดยไม่ต้องต่อ DB และไม่ติด login — ใช้เช็คว่า process ยังรับ request อยู่
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT}/static/app.css" > /dev/null || exit 1

# Render ยิง $PORT มาให้ตอน runtime — ต้องอ่านผ่าน shell form ไม่ใช่ exec form
CMD ["sh", "-c", "exec uvicorn ocrslip.web.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*'"]
