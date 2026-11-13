"""假的小米空气净化器（局域网 miIO 服务端），用来离线自测。

它按真实设备的线格式应答：握手回包 checksum = token，之后收发的载荷都是
AES-128-CBC 加密的 JSON。真机上不用跑它。

    python fake_purifier.py            # 监听 127.0.0.1:54321
"""

from __future__ import annotations

import json
import socket
import struct
import threading
import time
from typing import Any, Dict, Optional

import miio_lan as m

FAKE_MODEL = "zhimi.airpurifier.m2"
FAKE_DID = 0x1A2B3C4D


class FakePurifier:
    def __init__(self, host: str = "127.0.0.1", port: int = m.PORT,
                 token: str = "00112233445566778899aabbccddeeff") -> None:
        self.host = host
        self.port = port
        self.token = bytes.fromhex(token)
        self.state: Dict[str, Any] = {
            "power": "off",
            "mode": "auto",
            "favorite_level": 8,
            "motor1_speed": 0,
            "aqi": 12,
            "temp_dec": 246,
        }
        self.requests: list = []          # 收到的 (method, params)，便于断言
        self._sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.ts = int(time.time())

    # -- 生命周期 ---------------------------------------------------------
    def start(self) -> "FakePurifier":
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((self.host, self.port))
        self._sock.settimeout(0.3)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        if self._sock:
            self._sock.close()

    # -- 协议 -------------------------------------------------------------
    def _serve(self) -> None:
        assert self._sock is not None
        while not self._stop.is_set():
            try:
                data, addr = self._sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                reply = self._handle(data)
            except Exception:  # 坏包直接忽略
                continue
            if reply:
                self._sock.sendto(reply, addr)

    def _handle(self, data: bytes) -> Optional[bytes]:
        if len(data) < m.HEADER_LEN:
            return None
        magic, length, _, _, ts, _ = struct.unpack(">HHIII16s", data[:m.HEADER_LEN])
        if magic != m.MAGIC:
            return None

        if length == m.HEADER_LEN and data[2:4] == b"\x00\x20":
            # 握手
            self.ts = max(self.ts + 1, int(time.time()))
            header = struct.pack(">HHIII", m.MAGIC, m.HEADER_LEN, 0, FAKE_DID, self.ts)
            return header + self.token

        enc = data[m.HEADER_LEN:length]
        if m._md5(data[:16] + self.token + enc) != struct.unpack(">16s", data[16:32])[0]:
            return None  # 校验失败 = token 不对，真实设备同样不回包
        key, iv = m._derive_keys(self.token)
        payload = json.loads(m._unpad(m.aes_cbc_decrypt(enc, key, iv))
                             .rstrip(b"\x00").decode("utf-8"))
        result = self._dispatch(payload)
        self.ts = max(self.ts + 1, int(time.time()))

        body = m._pad(json.dumps({"id": payload.get("id"), "result": result},
                                 separators=(",", ":")).encode() + b"\x00")
        encrypted = m.aes_cbc_encrypt(body, key, iv)
        header = struct.pack(">HHIII", m.MAGIC, m.HEADER_LEN + len(encrypted), 0,
                             FAKE_DID, self.ts)
        return header + m._md5(header + self.token + encrypted) + encrypted

    def _dispatch(self, payload: Dict[str, Any]) -> Any:
        method = payload.get("method")
        params = payload.get("params") or []
        self.requests.append((method, params))

        if method == "miIO.info":
            return {"model": FAKE_MODEL, "fw_ver": "1.0.0", "mac": "AA:BB:CC:DD:EE:FF",
                    "token": self.token.hex()}
        if method == "get_prop":
            return [self.state.get(p) for p in params]
        if method == "set_power":
            self.state["power"] = params[0]
            return ["ok"]
        if method == "set_mode":
            self.state["mode"] = params[0]
            return ["ok"]
        if method in ("set_level_favorite", "set_level"):
            level = int(params[0])
            if not 0 <= level <= 17:
                return ["error"]
            self.state["favorite_level"] = level
            self.state["mode"] = "favorite"
            self.state["motor1_speed"] = 60 + level * 80 if self.state["power"] == "on" else 0
            return ["ok"]
        return ["unknown_method"]


if __name__ == "__main__":
    dev = FakePurifier().start()
    print("fake purifier on %s:%d, token=%s" % (dev.host, dev.port, dev.token.hex()))
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        dev.stop()
