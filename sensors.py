"""温度 / 负载采样。

支持三个来源，全部走公开接口，不需要第三方 Python 包：

* ``lhm``      —— LibreHardwareMonitor。优先读它的 Web Server (``/data.json``)，
                  读不到再用它的 WMI 命名空间 ``root/LibreHardwareMonitor``。
                  LHM 需要以管理员身份运行（读 CPU 温度要装内核驱动）。
* ``nvidia``   —— ``nvidia-smi``，任何权限都能读，N 卡机器开箱即用。
* ``cpu-load`` —— CPU 占用率（``Win32_PerfFormattedData_PerfOS_Processor``），
                  在拿不到温度时作为退化的热负荷代理。
"""

from __future__ import annotations

import json
import ctypes
import subprocess
import time
import urllib.request
from ctypes import wintypes
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

CREATE_NO_WINDOW = 0x08000000
PS = ["powershell", "-NoProfile", "-NonInteractive", "-Command",
      # 让 PowerShell 按 UTF-8 输出，否则中文错误信息会变乱码
      "$OutputEncoding=[Console]::OutputEncoding=[Text.Encoding]::UTF8;"]
NVIDIA_SMI = ["nvidia-smi",
              "--query-gpu=name,temperature.gpu,utilization.gpu,fan.speed",
              "--format=csv,noheader,nounits"]


class Reading(NamedTuple):
    name: str
    value: float


class SensorError(Exception):
    pass


def _run(cmd: List[str], timeout: float = 8.0) -> str:
    proc = subprocess.run(cmd, capture_output=True, timeout=timeout,
                          creationflags=CREATE_NO_WINDOW)
    if proc.returncode != 0:
        raw = (proc.stderr or proc.stdout or b"").decode("utf-8", "replace")
        first = next((ln.strip() for ln in raw.splitlines() if ln.strip()), "")
        raise SensorError("命令失败(%s): %s" % (proc.returncode, first[:200]))
    return proc.stdout.decode("utf-8", "replace")


# --------------------------------------------------------------------------
# LibreHardwareMonitor
# --------------------------------------------------------------------------
def _walk_lhm(node: Dict[str, Any], out: List[Reading]) -> None:
    value = node.get("Value") or node.get("value")
    text = node.get("Text") or node.get("Sensor") or node.get("Name") or ""
    if isinstance(value, str) and "°C" in value:
        try:
            out.append(Reading(str(text), float(value.replace("°C", "").strip())))
        except ValueError:
            pass
    for child in node.get("Children") or node.get("children") or []:
        _walk_lhm(child, out)


def lhm_web(host: str = "127.0.0.1", port: int = 8085,
            timeout: float = 3.0) -> List[Reading]:
    url = "http://%s:%d/data.json" % (host, port)
    with urllib.request.urlopen(url, timeout=timeout) as resp:  # nosec - 本机地址
        data = json.loads(resp.read().decode("utf-8", "replace"))
    out: List[Reading] = []
    _walk_lhm(data, out)
    if not out:
        raise SensorError("LHM Web Server 返回了数据，但没有温度项"
                          "（Options → 勾选 Web Server，并确认传感器已加载）")
    return out


_LHM_WMI_PS = r"""
$ErrorActionPreference = 'Stop'
$s = Get-CimInstance -Namespace 'root/LibreHardwareMonitor' -ClassName Sensor |
     Where-Object { $_.SensorType -eq 'Temperature' -and $_.Value -gt 0 } |
     Select-Object Name, Value
$s | ConvertTo-Json -Compress
"""


def lhm_wmi() -> List[Reading]:
    raw = _run(PS + [_LHM_WMI_PS])
    text = raw.strip()
    if not text:
        raise SensorError("LHM WMI 没有返回温度传感器（LHM 是否在运行？是否以管理员启动？）")
    data = json.loads(text)
    if isinstance(data, dict):
        data = [data]
    return [Reading(str(d.get("Name")), float(d.get("Value"))) for d in data
            if d.get("Value") is not None]


_wmi_supported: Optional[bool] = None


def lhm_read(host: str = "127.0.0.1", port: int = 8085) -> List[Reading]:
    """先试 Web Server（推荐），再试 WMI（只有旧版 LHM 才有）。"""
    errors = []
    try:
        return lhm_web(host, port)
    except Exception as ex:
        errors.append("Web Server %d: %s" % (port, ex))

    global _wmi_supported
    if _wmi_supported is not False:
        try:
            return lhm_wmi()
        except Exception as ex:
            errors.append("WMI: %s" % ex)
            msg = str(ex).lower()
            if "命名空间" in str(ex) or "namespace" in msg:
                # LHM 0.9.x 已经不带 WMI 提供程序，别每轮都白试一次
                _wmi_supported = False

    raise SensorError(
        "LibreHardwareMonitor 不可用（%s）。处理：右键「以管理员身份运行」LHM，"
        "再到它的 Options 里勾选 Web Server（端口 %d）" % ("; ".join(errors), port)
    )


# --------------------------------------------------------------------------
# NVIDIA
# --------------------------------------------------------------------------
_nvidia_cache: Dict[str, Any] = {"ts": 0.0, "data": None}


def nvidia(ttl: float = 6.0) -> List[Reading]:
    """nvidia-smi 每次都要起进程（约 50~100ms CPU），缓存几秒，省电也省风扇。"""
    now = time.time()
    cached = _nvidia_cache.get("data")
    if cached is not None and now - float(_nvidia_cache.get("ts", 0)) < ttl:
        return cached
    out = _run(NVIDIA_SMI)
    readings: List[Reading] = []
    for line in out.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 2:
            continue
        try:
            readings.append(Reading(parts[0], float(parts[1])))
        except ValueError:
            continue
    if not readings:
        raise SensorError("nvidia-smi 没有返回温度")
    _nvidia_cache["ts"] = now
    _nvidia_cache["data"] = readings
    return readings


# --------------------------------------------------------------------------
# CPU 占用率（退化方案）
# --------------------------------------------------------------------------
# --------------------------------------------------------------------------
# CPU 占用率：GetSystemTimes 差分（不开进程，几乎零开销）
# --------------------------------------------------------------------------
class _FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", wintypes.DWORD),
                ("dwHighDateTime", wintypes.DWORD)]


def _ft(ft: "_FILETIME") -> int:
    return (ft.dwHighDateTime << 32) | ft.dwLowDateTime


_last_sys_times: Optional[Tuple[int, int, int]] = None


def cpu_load() -> List[Reading]:
    global _last_sys_times
    idle, kernel, user = _FILETIME(), _FILETIME(), _FILETIME()
    if not ctypes.windll.kernel32.GetSystemTimes(
            ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
        raise SensorError("GetSystemTimes 失败")
    cur = (_ft(idle), _ft(kernel), _ft(user))
    prev, _last_sys_times = _last_sys_times, cur
    if prev is None:
        return [Reading("CPU Load", 0.0)]
    d_idle = cur[0] - prev[0]
    d_total = (cur[1] - prev[1]) + (cur[2] - prev[2])   # kernel 含 idle
    if d_total <= 0:
        return [Reading("CPU Load", 0.0)]
    busy = max(0, d_total - d_idle)
    return [Reading("CPU Load", max(0.0, min(100.0, 100.0 * busy / d_total)))]


READERS = {
    "lhm": lhm_read,
    "nvidia": nvidia,
    "cpu-load": cpu_load,
}


def read(kind: str, **kwargs) -> List[Reading]:
    reader = READERS.get(kind)
    if reader is None:
        raise SensorError("未知的传感器类型: %s（可选 %s）" % (kind, ", ".join(READERS)))
    if kind == "lhm":
        return reader(host=kwargs.get("host", "127.0.0.1"),
                      port=int(kwargs.get("port", 8085)))
    return reader()


def pick(readings: List[Reading], patterns: Optional[List[str]]) -> Optional[Reading]:
    """按名称/ID 关键字挑一个读数，取匹配项里的最高温度。"""
    if not readings:
        return None
    if not patterns:
        return max(readings, key=lambda r: r.value)
    for pat in patterns:
        hits = [r for r in readings if pat.lower() in r.name.lower()]
        if hits:
            return max(hits, key=lambda r: r.value)
    return None


if __name__ == "__main__":
    for kind in ("nvidia", "lhm", "cpu-load"):
        try:
            print(kind, "->", [tuple(r) for r in read(kind)])
        except Exception as ex:
            print(kind, "-> 不可用:", ex)
