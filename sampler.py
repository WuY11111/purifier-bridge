"""读传感器：把「哪个源、怎么兜底」的逻辑集中在一处。

之前 Controller 和界面各写了一份读取+兜底，改一处很容易漏另一处。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, List

import sensors

LOG = logging.getLogger("bridge")

# LHM 不可用时，用 CPU 占用率顶替温度（0~100% → 档位）
LOAD_FALLBACK_CURVE: List[List[int]] = [[10, 0], [35, 2], [55, 4], [75, 7], [90, 10], [100, 12]]

_fallback_logged = 0.0


@dataclass
class Sample:
    name: str
    source: str
    value: float
    fallback: bool = False      # True 表示这一路是降级数据（不是真实温度）


def read(spec: Dict[str, Any]) -> Sample:
    """读一路传感器；LHM 不可用时自动退化为 CPU 占用率。失败会抛异常。"""
    global _fallback_logged
    name = spec.get("name") or spec.get("kind", "?")
    kind = spec["kind"]

    try:
        readings = sensors.read(kind,
                                host=spec.get("host", "127.0.0.1"),
                                port=int(spec.get("port", 8085)))
        hit = sensors.pick(readings, spec.get("match"))
        if hit is None:
            raise sensors.SensorError(
                "没匹配到测点（可用：%s）" %
                (", ".join(r.name for r in readings) or "空"))
        return Sample(name, hit.name, hit.value)
    except Exception as ex:
        if kind != "lhm":
            raise
        try:
            load = sensors.pick(sensors.read("cpu-load"), None)
        except Exception:
            raise ex from None
        if load is None:
            raise
        now = time.time()
        if now - _fallback_logged > 120:      # 降级日志每 2 分钟最多一条
            _fallback_logged = now
            LOG.info("LHM 不可用，用 CPU 占用率顶替（%.0f%%）", load.value)
        return Sample(name, "CPU负载%", load.value, fallback=True)


def curve_for(spec: Dict[str, Any], sample: Sample) -> List[List[float]]:
    """降级数据要按负载曲线算，不能用温度曲线。"""
    return LOAD_FALLBACK_CURVE if sample.fallback else spec["curve"]
