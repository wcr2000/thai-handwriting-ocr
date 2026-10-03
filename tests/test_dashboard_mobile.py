"""Open the dashboard in a real browser in mobile mode and measure that it does not break.

Why a real browser is needed: layout bugs do not appear in the HTML the server emits. They appear
only once the browser has finished computing widths — an auto-fit short by 0.4px, say, dropping
the 4 tiles into a slab filling the screen, with the HTML identical character for character.

A caveat when taking screenshots by hand: `--headless --window-size=390,844` cannot measure
mobile. Chrome on macOS enforces a 500px minimum window width and then crops the image down to
390, so the page was laid out at 500px — the resulting screenshot looks as though the right edge
were cut off when in fact nothing overflows. Only Emulation.setDeviceMetricsOverride over CDP
yields a genuine 390 viewport.

These tests skip themselves when Chrome is absent or DATABASE_URL is unset, so CI is not required
to have a browser.
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
PHONE = (390, 844)          # iPhone 15 — the smallest screen the team actually uses
MIN_TAP_TARGET = 24         # the smallest tap target a finger reliably hits


def _chrome() -> str:
    for path in CHROME_CANDIDATES:
        if path and os.path.exists(path):
            return path
    pytest.skip("no Chrome on this machine")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server():
    """Start a real uvicorn, because TestClient exposes no socket for a browser to connect to"""
    if not DATABASE_URL:
        pytest.skip("DATABASE_URL is not set")
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
            pytest.skip("uvicorn did not come up within the timeout")
        yield f"http://127.0.0.1:{port}"
    finally:
        proc.terminate()
        proc.wait(timeout=10)


@pytest.fixture(scope="module")
def measure(server):
    """Return a function that takes JS and returns what that JS returns, from /dashboard in the emulated phone"""
    chrome = _chrome()
    token = auth.make_token(auth.User("admin", "admin"), SECRET_KEY)

    def _run(expression: str):
        return asyncio.run(_evaluate(chrome, server, token, expression))

    return _run


async def _evaluate(chrome: str, base: str, token: str, expression: str):
    import httpx
    import websockets

    port = _free_port()
    # A fresh profile every time: reusing the same folder makes Chrome serve the CSS it cached on a
    # previous run, so the tests go green over broken CSS (encountered while writing this file).
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
            pytest.skip("could not connect to CDP")
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
            # The crux of this file: a genuine mobile viewport size, not a cropped window
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


# ---------- the tests themselves ----------

def test_page_never_scrolls_sideways(measure):
    """The page must not be wider than the screen. app.css sets overflow-x:hidden on html, so
    anything overflowing does not become scrollable — it is silently clipped away, which is worse.
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
    assert got["overflowing"] == [], f"elements overflow the viewport: {got['overflowing']}"
    assert got["doc"] <= got["viewport"] + 1
    assert got["body"] <= got["viewport"] + 1


def test_stat_tiles_fit_two_per_row_on_a_phone(measure):
    """The tiles must come out as 2 columns, or 4 numbers alone fill the first screen and the charts take a scroll to reach.

    Very easy to break, because it depends on two numbers at once (the track minimum width and the
    gap) against the 367.6px main actually has on a 390px screen. Over by a fraction of a pixel and
    auto-fit immediately drops to one column, with the HTML unchanged character for character.
    """
    cols = measure(
        "getComputedStyle(document.querySelector('.tiles')).gridTemplateColumns.split(' ').length"
    )
    assert cols == 2, f"got {cols} column(s)"


def test_chart_columns_are_big_enough_to_tap(measure):
    """Each day's tap target is the whole column at full chart height, not just the ~10px-wide bar"""
    got = measure("""(() => {
      const c = document.querySelector('.dcol').getBoundingClientRect();
      return { w: Math.round(c.width), h: Math.round(c.height) };
    })()""")
    assert got["h"] >= MIN_TAP_TARGET
    assert got["w"] * got["h"] >= MIN_TAP_TARGET ** 2


def test_tooltips_stay_inside_the_screen(measure):
    """Tooltips on the leftmost and rightmost bars must not overflow the viewport, or they are clipped unreadable"""
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
    """There is no hover on a phone — tapping a bar must reveal that day's figures"""
    # The opacity transition has to finish first, or the value reads 0 even though the CSS is correct
    opacity = measure("""(async () => {
      const c = document.querySelectorAll('.dcol')[15];
      c.focus();
      await new Promise(r => setTimeout(r, 300));
      return getComputedStyle(c.querySelector('.tip')).opacity;
    })()""")
    assert float(opacity) > 0.9


def test_every_value_is_readable_without_hovering(measure):
    """A tooltip is a convenience, not the only way to read a value — every chart needs an accompanying table"""
    got = measure("""({
      charts: document.querySelectorAll('.panel').length,
      tables: document.querySelectorAll('details.tableview').length
    })""")
    assert got["tables"] >= got["charts"], got


def test_text_is_never_smaller_than_the_readable_floor(measure):
    """Text on a phone must not be smaller than 10px — below that it is genuinely unreadable"""
    smallest = measure("""(() => {
      let min = 99;
      document.querySelectorAll('.dash *').forEach(el => {
        if (!el.textContent.trim() || el.children.length) return;
        min = Math.min(min, parseFloat(getComputedStyle(el).fontSize));
      });
      return min;
    })()""")
    assert smallest >= 10, f"found text at {smallest}px"
