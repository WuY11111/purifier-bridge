#!/usr/bin/env python3
"""备选方案：用米家账号登录小米云，拉取局域网设备列表和 token。

只有在「局域网广播发现」不到设备时才需要它（比如路由器开了 AP 隔离 / 设备在别的网段，
此时即便拿到 token 也仍然连不上，本脚本会一并打印设备的 localip 方便你判断）。

    python 获取token-米家云.py

密码不会写进任何文件，只用于这一次登录。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from getpass import getpass

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def ensure_micloud() -> None:
    try:
        import micloud  # noqa: F401
        return
    except ImportError:
        pass
    print("需要安装 micloud（纯 Python 小库，用于登录小米云）。")
    if input("现在安装吗？[Y/n] ").strip().lower() in ("", "y", "yes"):
        subprocess.check_call([sys.executable, "-m", "pip", "install", "--user", "micloud"])
        import micloud  # noqa: F401
    else:
        raise SystemExit("已取消。可手动执行：%s -m pip install --user micloud" % sys.executable)


def login(cloud):
    try:
        cloud.login()
    except Exception as ex:
        msg = str(ex).lower()
        if "2fa" in msg or "verification" in msg or "code" in msg:
            code = input("需要验证码（短信/邮箱）：").strip()
            cloud.login(code=code)
        else:
            raise
    return cloud


def main() -> int:
    ensure_micloud()
    from micloud import MiCloud

    username = input("小米账号（手机号/邮箱）：").strip()
    password = getpass("密码（输入时不显示）：")
    cloud = login(MiCloud(username, password))

    devices = cloud.get_devices()
    print("\n=== 账号下的设备 ===")
    rows = []
    for dev in devices:
        rows.append({
            "name": dev.get("name"),
            "model": dev.get("model"),
            "did": dev.get("did"),
            "localip": dev.get("localip"),
            "token": dev.get("token"),
        })
    for r in rows:
        print("%-18s %-26s ip=%-15s token=%s" %
              (r["name"], r["model"], r["localip"] or "-", r["token"] or "-"))

    purifiers = [r for r in rows if "airpurifier" in str(r["model"]).lower()]
    if not purifiers:
        print("\n没找到空气净化器。请确认设备和账号在同一「家庭」下。")
        return 1
    print("\n=== 净化器 ===")
    for i, r in enumerate(purifiers):
        print("[%d] %s  ip=%s  token=%s" % (i, r["name"], r["localip"], r["token"]))

    if len(purifiers) == 1:
        pick = purifiers[0]
    else:
        idx = input("用第几个？[0] ").strip() or "0"
        pick = purifiers[int(idx)]

    cfg_path = os.path.join(BASE_DIR, "config.json")
    cfg = {}
    if os.path.exists(cfg_path):
        with open(cfg_path, "r", encoding="utf-8") as fh:
            cfg = json.load(fh)
    cfg.setdefault("purifier", {})
    if pick["token"]:
        cfg["purifier"]["token"] = pick["token"]
    if pick["localip"]:
        cfg["purifier"]["ip"] = pick["localip"]
    cfg["purifier"]["discovered_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(cfg_path, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False, indent=2)
    print("\n已写入 %s" % cfg_path)

    if not pick["localip"]:
        print("注意：云端的 localip 是空的（设备当前不在线）。先在米家 App 里确认设备在线。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
