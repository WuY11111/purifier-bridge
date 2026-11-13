"""极简 miIO 局域网协议客户端（仅依赖标准库）。

用来直接和「小米空气净化器 2」这类老一代 miIO 设备对话：
UDP 54321 + AES-128-CBC（密钥由设备 token 派生）。

线格式说明参考 python-miio（GPL-3.0）的 protocol.py / miioprotocol.py，
此处为独立实现，未包含其代码。

包结构（32 字节头 + 加密载荷）：
    0x2131 | length(u16) | unknown(u32) | device_id(u32) | timestamp(u32) | md5(16)
    checksum = md5(header[:16] + token + payload)
    payload  = AES-128-CBC( json + b"\x00", key=md5(token), iv=md5(key+token) )
握手包固定为 0x2131 0x0020 + 28 个 0xFF；设备回包的 checksum 字段就是 token。
"""

from __future__ import annotations

import hashlib
import json
import socket
import struct
import time
from typing import Any, Dict, List, Optional, Tuple

MAGIC = 0x2131
PORT = 54321
HELLO = bytes.fromhex("21310020" + "ff" * 28)
HEADER_LEN = 32


class MiioError(Exception):
    """设备返回错误或通信失败。"""


# --------------------------------------------------------------------------
# AES-128（FIPS-197）纯 Python 实现，S 盒由算法现算，避免抄错常量
# --------------------------------------------------------------------------
def _build_tables() -> Tuple[List[int], List[int]]:
    # 注意：GF(2^8)/0x11B 下 0x02 不是本原元（周期只有 51），
    # 必须用 0x03 才能生成完整的 255 阶循环表。
    exp = [0] * 512
    log = [0] * 256
    x = 1
    for i in range(255):
        exp[i] = x
        log[x] = i
        x ^= (x << 1) ^ (0x11B if x & 0x80 else 0)  # x *= 3
        x &= 0xFF
    for i in range(255, 512):
        exp[i] = exp[i - 255]

    def inv(a: int) -> int:
        return 0 if a == 0 else exp[255 - log[a]]

    def rotl(v: int, s: int) -> int:
        return ((v << s) | (v >> (8 - s))) & 0xFF

    sbox = [0] * 256
    for a in range(256):
        b = inv(a)
        sbox[a] = b ^ rotl(b, 1) ^ rotl(b, 2) ^ rotl(b, 3) ^ rotl(b, 4) ^ 0x63

    inv_sbox = [0] * 256
    for i, s in enumerate(sbox):
        inv_sbox[s] = i
    return sbox, inv_sbox


_SBOX, _INV_SBOX = _build_tables()
_RCON = [0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36]


def _gmul(a: int, b: int) -> int:
    """GF(2^8) 乘法（生成多项式 0x11B）。"""
    p = 0
    for _ in range(8):
        if b & 1:
            p ^= a
        hi = a & 0x80
        a = (a << 1) & 0xFF
        if hi:
            a ^= 0x1B
        b >>= 1
    return p


def _expand_key(key: bytes) -> List[List[int]]:
    if len(key) != 16:
        raise ValueError("AES-128 needs a 16-byte key")
    w = [list(key[i * 4:(i + 1) * 4]) for i in range(4)]
    for i in range(4, 44):
        t = list(w[i - 1])
        if i % 4 == 0:
            t = t[1:] + t[:1]
            t = [_SBOX[b] for b in t]
            t[0] ^= _RCON[i // 4 - 1]
        w.append([w[i - 4][j] ^ t[j] for j in range(4)])
    return w


def _add_round_key(state: List[int], w: List[List[int]], rnd: int) -> None:
    for c in range(4):
        for r in range(4):
            state[4 * c + r] ^= w[4 * rnd + c][r]


def _shift_rows(s: List[int], inverse: bool = False) -> List[int]:
    out = [0] * 16
    for r in range(4):
        for c in range(4):
            src = (c - r) % 4 if inverse else (c + r) % 4
            out[4 * c + r] = s[4 * src + r]
    return out


def _mix_columns(s: List[int], inverse: bool = False) -> List[int]:
    out = [0] * 16
    m = (14, 11, 13, 9) if inverse else (2, 3, 1, 1)
    for c in range(4):
        a = s[4 * c:4 * c + 4]
        for r in range(4):
            out[4 * c + r] = (
                _gmul(a[0], m[(0 - r) % 4])
                ^ _gmul(a[1], m[(1 - r) % 4])
                ^ _gmul(a[2], m[(2 - r) % 4])
                ^ _gmul(a[3], m[(3 - r) % 4])
            )
    return out


def _encrypt_block(block: bytes, w: List[List[int]]) -> bytes:
    s = list(block)
    _add_round_key(s, w, 0)
    for rnd in range(1, 10):
        s = [_SBOX[b] for b in s]
        s = _shift_rows(s)
        s = _mix_columns(s)
        _add_round_key(s, w, rnd)
    s = [_SBOX[b] for b in s]
    s = _shift_rows(s)
    _add_round_key(s, w, 10)
    return bytes(s)


def _decrypt_block(block: bytes, w: List[List[int]]) -> bytes:
    s = list(block)
    _add_round_key(s, w, 10)
    for rnd in range(9, 0, -1):
        s = _shift_rows(s, inverse=True)
        s = [_INV_SBOX[b] for b in s]
        _add_round_key(s, w, rnd)
        s = _mix_columns(s, inverse=True)
    s = _shift_rows(s, inverse=True)
    s = [_INV_SBOX[b] for b in s]
    _add_round_key(s, w, 0)
    return bytes(s)


def aes_cbc_encrypt(data: bytes, key: bytes, iv: bytes) -> bytes:
    if len(data) % 16:
        raise ValueError("data must be 16-byte aligned")
    w = _expand_key(key)
    out, prev = bytearray(), iv
    for i in range(0, len(data), 16):
        block = bytes(a ^ b for a, b in zip(data[i:i + 16], prev))
        prev = _encrypt_block(block, w)
        out += prev
    return bytes(out)


def aes_cbc_decrypt(data: bytes, key: bytes, iv: bytes) -> bytes:
    if len(data) % 16:
        raise ValueError("data must be 16-byte aligned")
    w = _expand_key(key)
    out, prev = bytearray(), iv
    for i in range(0, len(data), 16):
        block = data[i:i + 16]
        out += bytes(a ^ b for a, b in zip(_decrypt_block(block, w), prev))
        prev = block
    return bytes(out)


def _pad(data: bytes) -> bytes:
    n = 16 - len(data) % 16
    return data + bytes([n]) * n


def _unpad(data: bytes) -> bytes:
    if not data:
        return data
    n = data[-1]
    if 1 <= n <= 16 and data[-n:] == bytes([n]) * n:
        return data[:-n]
    return data


def _md5(data: bytes) -> bytes:
    return hashlib.md5(data).digest()  # nosec - miIO 协议规定


_SIO_UDP_CONNRESET = getattr(socket, "SIO_UDP_CONNRESET", 0x9800000C)


def new_udp_socket() -> socket.socket:
    """UDP socket；Windows 上关闭 SIO_UDP_CONNRESET。

    否则给没人监听的地址发包后，recvfrom 会抛 WinError 10054
    （"远程主机强迫关闭了一个现有的连接"，由 ICMP 端口不可达引起），
    会干扰设备扫描和离线重试。
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    for opt in (struct.pack("<?", False), False):
        try:
            s.ioctl(_SIO_UDP_CONNRESET, opt)
            break
        except Exception:
            continue
    return s


def _recvfrom(sock: socket.socket, size: int, tries: int = 5) -> Tuple[bytes, Tuple[str, int]]:
    """recvfrom，忽略 Windows 上由 ICMP 不可达引发的 10054。"""
    for _ in range(tries):
        try:
            return sock.recvfrom(size)
        except ConnectionResetError:
            continue
    raise MiioError("接收失败：连接被重置")


def _derive_keys(token: bytes) -> Tuple[bytes, bytes]:
    key = _md5(token)
    iv = _md5(key + token)
    return key, iv


# --------------------------------------------------------------------------
# 设备
# --------------------------------------------------------------------------
class MiioDevice:
    """一台 miIO 设备（IP + 32 位十六进制 token）。"""

    def __init__(self, ip: str, token: str, timeout: float = 4.0) -> None:
        self.ip = ip
        self.token = bytes.fromhex(token.strip())
        if len(self.token) != 16:
            raise ValueError("token 必须是 32 个十六进制字符")
        self.timeout = timeout
        self.device_id: Optional[int] = None
        self._ts: int = 0
        self._msg_id: int = 0
        self._last_handshake: float = 0.0

    # -- 底层 -------------------------------------------------------------
    def _socket(self) -> socket.socket:
        s = new_udp_socket()
        s.settimeout(self.timeout)
        return s

    def handshake(self) -> Dict[str, Any]:
        """握手，返回 device_id / 时间戳 / 回包中的 token。"""
        with self._socket() as s:
            s.sendto(HELLO, (self.ip, PORT))
            data, _ = _recvfrom(s, 1024)
        if len(data) < HEADER_LEN:
            raise MiioError("握手回包过短")
        magic, length, _, did, ts, checksum = struct.unpack(">HHIII16s", data[:HEADER_LEN])
        if magic != MAGIC:
            raise MiioError("不是 miIO 设备（magic=0x%x）" % magic)
        self.device_id = did
        self._ts = ts
        self._last_handshake = time.time()
        return {"device_id": did, "ts": ts, "token": checksum.hex()}

    def _need_handshake(self) -> bool:
        return self.device_id is None or (time.time() - self._last_handshake) > 300

    def send(self, method: str, params: Optional[Any] = None, retries: int = 2) -> Any:
        """发送一条 RPC 并返回 result。"""
        last_err: Optional[Exception] = None
        for attempt in range(retries + 1):
            try:
                if self._need_handshake():
                    self.handshake()
                payload = {
                    "id": self._next_id(),
                    "method": method,
                    "params": [] if params is None else params,
                }
                return self._transact(payload)
            except (OSError, MiioError) as ex:  # 超时 / 设备忙 → 重握手重试
                last_err = ex
                self.device_id = None
                if attempt < retries:
                    time.sleep(0.6 * (attempt + 1))
        raise MiioError("发送 %s 失败: %s" % (method, last_err))

    def _next_id(self) -> int:
        self._msg_id += 1
        if self._msg_id >= 9999:
            self._msg_id = 1
        return self._msg_id

    def _transact(self, payload: Dict[str, Any]) -> Any:
        if self.device_id is None:
            raise MiioError("尚未握手")
        key, iv = _derive_keys(self.token)
        body = _pad(json.dumps(payload, separators=(",", ":")).encode() + b"\x00")
        encrypted = aes_cbc_encrypt(body, key, iv)

        self._ts += 1
        header = struct.pack(">HHIII", MAGIC, HEADER_LEN + len(encrypted), 0,
                             self.device_id, self._ts)
        checksum = _md5(header + self.token + encrypted)
        packet = header + checksum + encrypted

        with self._socket() as s:
            s.sendto(packet, (self.ip, PORT))
            data, _ = _recvfrom(s, 4096)

        if len(data) < HEADER_LEN:
            raise MiioError("回包过短")
        magic, length, _, did, ts, recv_sum = struct.unpack(">HHIII16s", data[:HEADER_LEN])
        if magic != MAGIC:
            raise MiioError("回包 magic 异常")
        enc = data[HEADER_LEN:length] if length > HEADER_LEN else b""
        if _md5(data[:16] + self.token + enc) != recv_sum:
            raise MiioError("校验失败：token 不对，或设备已重置 token")
        self._ts = ts

        plain = _unpad(aes_cbc_decrypt(enc, key, iv)).rstrip(b"\x00")
        try:
            resp = json.loads(plain.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as ex:
            raise MiioError("回包解密后不是 JSON: %s" % ex) from ex

        if resp.get("error"):
            err = resp["error"]
            raise MiioError("设备返回错误 %s: %s" % (err.get("code"), err.get("message")))
        return resp.get("result", resp)

    # -- 高层 -------------------------------------------------------------
    def info(self) -> Dict[str, Any]:
        return self.send("miIO.info", [])

    def get_prop(self, props: List[str]) -> List[Any]:
        out: List[Any] = []
        for i in range(0, len(props), 15):  # 单次请求上限 16 个属性
            out += self.send("get_prop", props[i:i + 15])
        return out

    def status(self) -> Dict[str, Any]:
        keys = ["power", "mode", "favorite_level", "motor1_speed", "aqi", "temp_dec"]
        return dict(zip(keys, self.get_prop(keys)))

    def set_power(self, on: bool) -> Any:
        return self.send("set_power", ["on" if on else "off"])

    def set_mode(self, mode: str) -> Any:
        return self.send("set_mode", [mode])

    def set_buzzer(self, on: bool) -> Any:
        """开关按键/提示音（"滴"的声源）。"""
        return self.send("set_buzzer", ["on" if on else "off"])

    def set_favorite_level(self, level: int, command: str = "set_level_favorite") -> Any:
        return self.send(command, [int(level)])


def local_subnets() -> List[str]:
    """本机所有 IPv4 网段的 /24 前缀（例如 ["192.168.31."]）。

    用来做局域网扫描：很多路由器会吞掉子网广播，逐 IP 单播最可靠。
    """
    prefixes = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip.startswith(("127.", "169.254.")):
                continue
            prefixes.add(ip.rsplit(".", 1)[0] + ".")
    except Exception:
        pass
    if not prefixes:                      # 兜底：拿默认出口地址
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 53))
            prefixes.add(s.getsockname()[0].rsplit(".", 1)[0] + ".")
            s.close()
        except Exception:
            pass
    return sorted(prefixes)

def discover(timeout: float = 3.0, with_info: bool = True,
             subnets: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """发现局域网内的 miIO 设备；握手回包里的 checksum 就是 token。

    subnets 例如 ["192.168.31."]，会额外发子网广播并逐个 IP 单播
    （很多路由器/AP 会吞掉子网广播，逐 IP 敲最可靠）。
    """
    found: List[Dict[str, Any]] = []
    seen = set()
    s = new_udp_socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.settimeout(1.0)
    prefixes = [p if p.endswith(".") else p + "." for p in (subnets or local_subnets())]
    try:
        for _ in range(3):
            try:
                s.sendto(HELLO, ("255.255.255.255", PORT))
                for pre in prefixes:
                    s.sendto(HELLO, (pre + "255", PORT))
            except OSError:
                pass
        for pre in prefixes:
            for i in range(1, 255):
                try:
                    s.sendto(HELLO, ("%s%d" % (pre, i), PORT))
                except OSError:
                    break

        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                data, addr = s.recvfrom(1024)
            except socket.timeout:
                continue
            except ConnectionResetError:
                continue
            except OSError:
                break
            if len(data) < HEADER_LEN or addr[0] in seen:
                continue
            magic, _, _, did, ts, token = struct.unpack(">HHIII16s", data[:HEADER_LEN])
            if magic != MAGIC:
                continue
            seen.add(addr[0])
            found.append({"ip": addr[0], "device_id": did, "token": token.hex()})

        # 补设备信息要放到接收循环之外：info() 会阻塞 1~2 秒，
        # 放在循环里会把后面到达的回包挤掉（实测只能发现第一台）。
        if with_info:
            for entry in found:
                if entry["token"] == "ff" * 16:
                    entry["info_error"] = "token 全 ff，不是可控制的 miIO 设备"
                    continue
                try:
                    entry["info"] = MiioDevice(entry["ip"], entry["token"],
                                               timeout=2.0).info()
                except Exception as ex:  # 设备可能不支持 info，忽略
                    entry["info_error"] = str(ex)
    finally:
        s.close()
    return found


if __name__ == "__main__":
    for dev in discover():
        print(json.dumps(dev, ensure_ascii=False, indent=2))
