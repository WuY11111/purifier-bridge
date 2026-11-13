"""配置：默认值、读写、归一化。

所有“默认长什么样”的知识都集中在这里，其他模块只管拿 dict 用。
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict

CONFIG_VERSION = 1

# 12700H 实测：空载 48℃、全核满载 94~96℃；RTX 3060 Laptop 空载 48~52℃、满载 75~85℃
DEFAULT_CONFIG: Dict[str, Any] = {
    "config_version": CONFIG_VERSION,
    "purifier": {
        "ip": "",
        "token": "",
        "timeout_s": 4,
        "level_command": "set_level_favorite",
        "silent": True,
        "wifi_profile": "",
    },
    "control": {
        "interval_s": 10,
        "min_level": 0,
        "max_level": 16,
        "deadband": 2,
        "min_command_gap_s": 30,
        "ema_alpha": 0.35,
        "idle_release_min": 10,
        "idle_action": "auto",
        "active_hours": [],
        "restore_on_exit": True,
    },
    "sensors": [
        {
            "name": "cpu",
            "kind": "lhm",
            "match": ["CPU Package", "Core Max", "Core (Tctl", "CPU Total"],
            "curve": [[52, 0], [60, 2], [68, 4], [76, 7], [83, 10], [88, 13], [94, 16]],
        },
        {
            "name": "gpu",
            "kind": "nvidia",
            "match": [],
            "curve": [[45, 0], [52, 2], [58, 4], [64, 7], [70, 10], [76, 13], [82, 16]],
        },
    ],
}


def _blank() -> Dict[str, Any]:
    """深拷贝一份默认配置（避免调用方改到模块常量）。"""
    return json.loads(json.dumps(DEFAULT_CONFIG))


def normalize(raw: Dict[str, Any]) -> Dict[str, Any]:
    """补默认值 + 收敛类型/范围；不认识的字段原样保留。"""
    cfg = _blank()
    cfg.update({k: v for k, v in (raw or {}).items()
                if k not in ("purifier", "control", "sensors")})
    cfg["purifier"].update(raw.get("purifier") or {})
    cfg["control"].update(raw.get("control") or {})
    if raw.get("sensors"):
        cfg["sensors"] = raw["sensors"]

    c = cfg["control"]
    try:
        c["interval_s"] = max(2.0, float(c.get("interval_s", 10)))
    except (TypeError, ValueError):
        c["interval_s"] = 10.0
    try:
        c["min_command_gap_s"] = max(0.0, float(c.get("min_command_gap_s", 30)))
    except (TypeError, ValueError):
        c["min_command_gap_s"] = 30.0
    try:
        c["ema_alpha"] = min(1.0, max(0.05, float(c.get("ema_alpha", 0.35))))
    except (TypeError, ValueError):
        c["ema_alpha"] = 0.35
    try:
        c["min_level"] = max(0, min(17, int(c.get("min_level", 0))))
        c["max_level"] = max(0, min(17, int(c.get("max_level", 16))))
    except (TypeError, ValueError):
        c["min_level"], c["max_level"] = 0, 16
    if c["max_level"] < c["min_level"]:
        c["max_level"] = c["min_level"]
    try:
        c["deadband"] = max(1, int(c.get("deadband", 2)))
    except (TypeError, ValueError):
        c["deadband"] = 2

    cfg["config_version"] = CONFIG_VERSION
    return cfg


def load_config(path: str, create: bool = True) -> Dict[str, Any]:
    if not os.path.exists(path):
        if not create:
            raise SystemExit("找不到配置文件：%s" % path)
        save_config(_blank(), path)
        return _blank()
    with open(path, "r", encoding="utf-8-sig") as fh:
        raw = json.load(fh)
    return normalize(raw)


def save_config(cfg: Dict[str, Any], path: str) -> None:
    tmp = str(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)      # 原子写，断电也不会写坏配置
