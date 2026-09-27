"""เปิดหน้าสรุปด้วยเบราว์เซอร์จริงในโหมดมือถือ แล้ววัดว่ามันไม่พัง

ทำไมต้องใช้เบราว์เซอร์จริง: บั๊ก layout ไม่โผล่ใน HTML ที่ server ส่งออกมา
มันโผล่ตอนเบราว์เซอร์คำนวณความกว้างเสร็จแล้วเท่านั้น เช่น auto-fit ที่ขาดไป 0.4px
แล้วไทล์ 4 ใบตกลงมาเรียงเป็นตับเต็มหน้าจอ — HTML เหมือนเดิมทุกตัวอักษร

ข้อควรระวังตอนถ่ายภาพหน้าจอเอง: `--headless --window-size=390,844` ใช้วัดมือถือไม่ได้
Chrome บน macOS บังคับความกว้างหน้าต่างขั้นต่ำ 500px แล้วค่อยครอปรูปให้เหลือ 390
หน้าจึงถูก layout ที่ 500px — ภาพที่ได้ดูเหมือนขอบขวาโดนตัด ทั้งที่จริงไม่มีอะไรล้น
ต้องสั่ง Emulation.setDeviceMetricsOverride ผ่าน CDP เท่านั้นถึงจะได้ viewport 390 จริง

เทสนี้ข้ามเองถ้าไม่มี Chrome หรือไม่ได้ตั้ง DATABASE_URL จึงไม่บังคับให้ CI ต้องมีเบราว์เซอร์
"""

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time

import pytest

from ocrslip import auth
from ocrslip.config import DATABASE_URL, SECRET_KEY

CHROME_CANDIDATES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    shutil.which("google-chrome") or "",
    shutil.which("chromium") or "",
)
PHONE = (390, 844)          # iPhone 15 — จอเล็กสุดที่ทีมใช้จริง
MIN_TAP_TARGET = 24         # ขนาดพื้นที่แตะขั้นต่ำที่นิ้วกดโดน


def _chrome() -> str:
    for path in CHROME_CANDIDATES:
        if path and os.path.exists(path):
            return path
    pytest.skip("ไม่มี Chrome ในเครื่องนี้")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server():
    """ยิง uvicorn จริงขึ้นมา เพราะ TestClient ไม่มี socket ให้เบราว์เซอร์ต่อ"""
    if not DATABASE_URL:
        pytest.skip("ไม่ได้ตั้ง DATABASE_URL")
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "ocrslip.web.main:app", "--port", str(port)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        import httpx

        for _ in range(100):
            try:
                if httpx.get(f"http://127.0.0.1:{port}/health", timeout=1).status_code == 200:
                    break
            except Exception:
                time.sleep(0.2)
        else:
            pytest.skip("uvicorn ไม่ขึ้นภายในเวลาที่รอ")
        yield f"http://127.0.0.1:{port}"
    finally:
        proc.terminate()
        proc.wait(timeout=10)


@pytest.fixture(scope="module")
def measure(server):
    """คืนฟังก์ชันที่รับ JS แล้วคืนค่าที่ JS นั้น return จากหน้า /dashboard บนมือถือจำลอง"""
    chrome = _chrome()
    token = auth.make_token(auth.User("admin", "admin"), SECRET_KEY)

    def _run(expression: str):
        return asyncio.run(_evaluate(chrome, server, token, expression))

    return _run


async def _evaluate(chrome: str, base: str, token: str, expression: str):
    import httpx
    import websockets

    port = _free_port()
    # โปรไฟล์ใหม่ทุกครั้ง — ถ้าใช้โฟลเดอร์เดิมซ้ำ Chrome จะหยิบ CSS ที่ cache ไว้รอบก่อนมาใช้
    # แล้วเทสจะเขียวทั้งที่แก้ CSS พัง (เจอมาแล้วตอนเขียนไฟล์นี้)
    profile = tempfile.TemporaryDirectory(prefix="ocrslip-cdp-")
    proc = subprocess.Popen(
        [chrome, "--headless=new", "--disable-gpu", f"--remote-debugging-port={port}",
         f"--user-data-dir={profile.name}", "--no-first-run", "--disable-application-cache",
         "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(100):
            try:
                tabs = httpx.get(f"http://127.0.0.1:{port}/json", timeout=1).json()
                break
            except Exception:
                time.sleep(0.2)
        else:
            pytest.skip("ต่อ CDP ไม่ติด")
        ws_url = next(t["webSocketDebuggerUrl"] for t in tabs if t["type"] == "page")
        async with websockets.connect(ws_url, max_size=50_000_000) as ws:
            seq = [0]

            async def call(method, **params):
                seq[0] += 1
                await ws.send(json.dumps({"id": seq[0], "method": method, "params": params}))
                while True:
                    msg = json.loads(await ws.recv())
                    if msg.get("id") == seq[0]:
                        assert "error" not in msg, msg["error"]
                        return msg.get("result", {})

            await call("Page.enable")
            await call("Network.enable")
            await call("Network.setCacheDisabled", cacheDisabled=True)
            # จุดสำคัญของไฟล์นี้ — ขนาด viewport จริงของมือถือ ไม่ใช่หน้าต่างที่ถูกครอป
            await call("Emulation.setDeviceMetricsOverride",
                       width=PHONE[0], height=PHONE[1], deviceScaleFactor=2, mobile=True)
            await call("Emulation.setFocusEmulationEnabled", enabled=True)
            await call("Network.setCookie", name=auth.COOKIE_NAME, value=token,
                       domain="127.0.0.1", path="/")
            await call("Page.navigate", url=f"{base}/dashboard")
            await asyncio.sleep(2.0)
            out = await call("Runtime.evaluate", returnByValue=True, awaitPromise=True,
                             expression=expression)
            assert "exceptionDetails" not in out, out.get("exceptionDetails")
            return out["result"]["value"]
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        profile.cleanup()


# ---------- เทสจริง ----------

def test_page_never_scrolls_sideways(measure):
    """หน้าห้ามกว้างกว่าจอ — app.css ตั้ง overflow-x:hidden ไว้ที่ html
    ของที่ล้นจึงไม่ทำให้เลื่อนได้ แต่จะ "โดนตัดหาย" เงียบ ๆ ซึ่งแย่กว่า
    """
    got = measure("""({
      viewport: innerWidth,
      doc: document.documentElement.scrollWidth,
      body: document.body.scrollWidth,
      overflowing: [...document.querySelectorAll('body *')]
        .filter(el => el.getBoundingClientRect().right > innerWidth + 1
                      && getComputedStyle(el).position !== 'absolute')
        .slice(0, 5).map(el => el.tagName + '.' + el.className)
    })""")
    assert got["overflowing"] == [], f"มี element ล้นขอบจอ: {got['overflowing']}"
    assert got["doc"] <= got["viewport"] + 1
    assert got["body"] <= got["viewport"] + 1


def test_stat_tiles_fit_two_per_row_on_a_phone(measure):
    """ไทล์ต้องได้ 2 คอลัมน์ ไม่งั้นแค่ 4 ตัวเลขก็กินทั้งหน้าจอแรกจนต้องเลื่อนหากราฟ

    พังง่ายมากเพราะขึ้นกับเลขสองตัวพร้อมกัน (ความกว้างขั้นต่ำของ track กับ gap)
    เทียบกับพื้นที่จริงใน main ที่เหลือ 367.6px บนจอ 390px — เกินไปเศษ px เดียว
    auto-fit ก็ตัดเหลือคอลัมน์เดียวทันที โดย HTML ไม่เปลี่ยนสักตัวอักษร
    """
    cols = measure(
        "getComputedStyle(document.querySelector('.tiles')).gridTemplateColumns.split(' ').length"
    )
    assert cols == 2, f"ได้ {cols} คอลัมน์"


def test_chart_columns_are_big_enough_to_tap(measure):
    """พื้นที่แตะของแต่ละวันคือทั้งคอลัมน์เต็มความสูงกราฟ ไม่ใช่เฉพาะแท่งที่กว้าง ~10px"""
    got = measure("""(() => {
      const c = document.querySelector('.dcol').getBoundingClientRect();
      return { w: Math.round(c.width), h: Math.round(c.height) };
    })()""")
    assert got["h"] >= MIN_TAP_TARGET
    assert got["w"] * got["h"] >= MIN_TAP_TARGET ** 2


def test_tooltips_stay_inside_the_screen(measure):
    """ป้ายของแท่งริมซ้าย/ขวาต้องไม่ล้นขอบจอ ไม่งั้นจะโดนตัดจนอ่านไม่ครบ"""
    got = measure("""(() => {
      const cols = [...document.querySelectorAll('.dcol')];
      return [cols[0], cols[1], cols.at(-2), cols.at(-1)].map(c => {
        const t = c.querySelector('.tip').getBoundingClientRect();
        return { left: Math.round(t.left), right: Math.round(t.right) };
      });
    })()""")
    for box in got:
        assert box["left"] >= -1, box
        assert box["right"] <= PHONE[0] + 1, box


def test_tapping_a_column_reveals_its_numbers(measure):
    """บนมือถือไม่มี hover — แตะแท่งแล้วต้องเห็นตัวเลขของวันนั้น"""
    # ต้องรอให้ transition ของ opacity วิ่งจบก่อน ไม่งั้นอ่านค่าได้ 0 ทั้งที่ CSS ถูกแล้ว
    opacity = measure("""(async () => {
      const c = document.querySelectorAll('.dcol')[15];
      c.focus();
      await new Promise(r => setTimeout(r, 300));
      return getComputedStyle(c.querySelector('.tip')).opacity;
    })()""")
    assert float(opacity) > 0.9


def test_every_value_is_readable_without_hovering(measure):
    """tooltip เป็นของแถม ไม่ใช่ทางเดียวที่จะอ่านค่า — ทุกกราฟต้องมีตารางกำกับ"""
    got = measure("""({
      charts: document.querySelectorAll('.panel').length,
      tables: document.querySelectorAll('details.tableview').length
    })""")
    assert got["tables"] >= got["charts"], got


def test_text_is_never_smaller_than_the_readable_floor(measure):
    """ตัวหนังสือบนมือถือห้ามเล็กกว่า 10px — เล็กกว่านี้คืออ่านไม่ออกจริง ๆ"""
    smallest = measure("""(() => {
      let min = 99;
      document.querySelectorAll('.dash *').forEach(el => {
        if (!el.textContent.trim() || el.children.length) return;
        min = Math.min(min, parseFloat(getComputedStyle(el).fontSize));
      });
      return min;
    })()""")
    assert smallest >= 10, f"เจอตัวหนังสือขนาด {smallest}px"
