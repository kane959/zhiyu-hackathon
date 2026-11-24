#!/usr/bin/env python3
"""Full-page screenshot at an exact CSS viewport, for mobile review.

Why this exists: `chrome --headless --window-size=390,844` does not produce a
390px layout viewport on Windows. The window has a minimum width, so the page
lays out at about 512 CSS pixels and the PNG is a crop of it — narrow layouts
look broken when they are fine. Driving the DevTools protocol sets the layout
viewport directly, which is what a phone actually reports.

    python scripts/shot.py http://127.0.0.1:4173/recover out.png 390 844 --mobile
    python scripts/shot.py http://127.0.0.1:4173/t/<slug> out.png 1280 900
    python scripts/shot.py URL out.png 390 844 --mobile --eval "document.querySelector('#issue').click()"

Exit code is non-zero when Chrome cannot be found or the page fails to load, so
a broken check does not silently produce an empty PNG.

Standard library only. The WebSocket framing below is the subset CDP needs.
"""
import base64
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORT = 9333

CANDIDATES = [
    os.environ.get("ZHIYU_CHROME", ""),
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
]


def find_chrome():
    for candidate in CANDIDATES:
        if candidate and Path(candidate).is_file():
            return candidate
    for name in ("google-chrome", "chromium", "chrome", "msedge"):
        found = shutil.which(name)
        if found:
            return found
    raise SystemExit("找不到 Chrome 或 Edge；用环境变量 ZHIYU_CHROME 指定可执行文件路径。")


def http_json(path):
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}{path}", timeout=10) as response:
        return json.load(response)


class WS:
    def __init__(self, host, port, path):
        self.sock = socket.create_connection((host, port), timeout=30)
        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(request.encode())
        buffer = b""
        while b"\r\n\r\n" not in buffer:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise RuntimeError("websocket handshake closed")
            buffer += chunk
        head, self.buffer = buffer.split(b"\r\n\r\n", 1)
        if b"101" not in head.split(b"\r\n")[0]:
            raise RuntimeError("websocket handshake failed: " + head.decode(errors="replace"))
        self.next_id = 0

    def send(self, text):
        data = text.encode()
        header = bytearray([0x81])
        length = len(data)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", length)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", length)
        mask = os.urandom(4)
        header += mask
        self.sock.sendall(bytes(header) + bytes(byte ^ mask[i % 4] for i, byte in enumerate(data)))

    def _read(self, size):
        while len(self.buffer) < size:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise RuntimeError("websocket closed")
            self.buffer += chunk
        out, self.buffer = self.buffer[:size], self.buffer[size:]
        return out

    def recv(self):
        first, second = self._read(2)
        opcode = first & 0x0F
        length = second & 0x7F
        if length == 126:
            length = struct.unpack(">H", self._read(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", self._read(8))[0]
        masked = second & 0x80
        mask = self._read(4) if masked else None
        payload = self._read(length)
        if mask:
            payload = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
        return opcode, payload

    def call(self, method, params=None):
        self.next_id += 1
        wanted = self.next_id
        self.send(json.dumps({"id": wanted, "method": method, "params": params or {}}))
        while True:
            opcode, payload = self.recv()
            if opcode == 0x8:
                raise RuntimeError("websocket closed by peer")
            if opcode != 0x1:
                continue
            message = json.loads(payload)
            if message.get("id") == wanted:
                if "error" in message:
                    raise RuntimeError(f"{method}: {message['error']}")
                return message.get("result", {})


def capture(url, out, width, height, mobile, script="", wait=2.5):
    profile = tempfile.mkdtemp(prefix="zhiyu-shot-")
    chrome = subprocess.Popen(
        [
            find_chrome(),
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            "--hide-scrollbars",
            f"--remote-debugging-port={PORT}",
            f"--user-data-dir={profile}",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.time() + 25
        while True:
            try:
                targets = http_json("/json/list")
                break
            except Exception:
                if time.time() > deadline:
                    raise SystemExit("Chrome 调试端口没有起来，检查是否有残留进程占用 9333。")
                time.sleep(0.3)
        page = next(item for item in targets if item["type"] == "page")
        path = page["webSocketDebuggerUrl"].split(f":{PORT}")[-1]
        ws = WS("127.0.0.1", PORT, path)

        ws.call("Page.enable")
        ws.call(
            "Emulation.setDeviceMetricsOverride",
            {"width": width, "height": height, "deviceScaleFactor": 2, "mobile": mobile},
        )
        ws.call("Page.navigate", {"url": url})
        time.sleep(wait)  # the app fetches before the first meaningful paint
        if script:
            ws.call("Runtime.evaluate", {"expression": script, "awaitPromise": True})
            time.sleep(1.5)
        shot = ws.call("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": True})
        target = Path(out)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(base64.b64decode(shot["data"]))
        print(f"{target}  viewport={width}x{height} mobile={mobile}")
    finally:
        chrome.terminate()
        chrome.wait(timeout=10)
        shutil.rmtree(profile, ignore_errors=True)


def main():
    positional, script = [], ""
    args = sys.argv[1:]
    index = 0
    while index < len(args):
        if args[index] == "--eval":
            script = args[index + 1]
            index += 2
            continue
        if not args[index].startswith("--"):
            positional.append(args[index])
        index += 1

    if len(positional) < 2:
        raise SystemExit(__doc__.strip().splitlines()[0] + "\n用法见文件头。")
    url, out = positional[0], positional[1]
    width = int(positional[2]) if len(positional) > 2 else 390
    height = int(positional[3]) if len(positional) > 3 else 844
    capture(url, out, width, height, "--mobile" in sys.argv, script)


if __name__ == "__main__":
    main()
