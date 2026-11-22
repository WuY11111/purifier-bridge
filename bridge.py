#!/usr/bin/env python3
"""笔记本温度 → 小米空气净化器 2 风速，闭环控制。

    python bridge.py discover    # 扫描局域网，拿到净化器的 IP 和 token
    python bridge.py probe       # 看看本机有哪些温度传感器可用
    python bridge.py once        # 按当前温度算一次并下发（调试用）
    python bridge.py run         # 常驻：每 interval_s 秒调一次风速
    python bridge.py status      # 读净化器当前状态
    python bridge.py set 12      # 手动设到 12 档
    python bridge.py selftest    # 用内置假设备做协议自检，不需要真机
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time
from logging.handlers import RotatingFileHandler
from typing import Any, Dict, List, Optional, Tuple

import miio_lan
import sampler
import sensors
from config import load_config, save_config

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

LOG_DIR = os.path.join(BASE_DIR, "logs")

LOG = logging.getLogger("bridge")

# Windows 控制台默认是 GBK，中文日志会乱码（尤其在 pwsh / 重定向到文件时）
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass




# --------------------------------------------------------------------------
# 曲线
# --------------------------------------------------------------------------
def format_value(value: float, source: str) -> str:
    """温度显示 ℃，负载显示 %。"""
    return "%.0f%%" % value if ("负载" in source or "%" in source) else "%.1f°C" % value


def curve_value(points: List[List[float]], x: float) -> float:
    """折线插值：温度 x 落在哪一段就按哪一段线性映射到档位。"""
    pts = sorted((float(a), float(b)) for a, b in points)
    if len(pts) == 1:
        return pts[0][1]
    if x <= pts[0][0]:
        return pts[0][1]
    if x >= pts[-1][0]:
        return pts[-1][1]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x0 <= x <= x1:
            if x1 == x0:
                return y1
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return pts[-1][1]


# --------------------------------------------------------------------------
# 控制器
# --------------------------------------------------------------------------
class Controller:
    def __init__(self, cfg: Dict[str, Any], dry_run: bool = False,
                 config_path: Optional[str] = None) -> None:
        self.cfg = cfg
        self.cfg_path = config_path or CONFIG_PATH
        self.fail_count = 0
        self.next_retry = 0.0
        self.mac = str(cfg["purifier"].get("mac", "")).lower()
        self.silent = bool(cfg["purifier"].get("silent", True))
        self._silenced = False
        self._buzzer_saved: Optional[str] = None
        self.control = cfg["control"]
        self.dry_run = dry_run
        self.device: Optional[miio_lan.MiioDevice] = None
        self.level_command: str = cfg["purifier"].get("level_command", "set_level_favorite")
        self.ema: Dict[str, float] = {}
        self.last_level: Optional[int] = None
        self.last_cmd_ts: float = 0.0
        self.idle_since: Optional[float] = None
        self._fail_until: Dict[str, float] = {}
        self._released = False
        self.last: Dict[str, Any] = {}   # 最近一次决策，给图形界面读
        self._ensure_device()

    # -- 设备 -------------------------------------------------------------
    def _ensure_device(self) -> None:
        p = self.cfg["purifier"]
        if not p.get("ip") or not p.get("token"):
            if self.dry_run:
                LOG.warning("配置里没有 ip/token；dry-run 只算档位，不连设备")
                return
            LOG.info("配置里没有 ip/token，尝试局域网自动发现……")
            found = miio_lan.discover(timeout=3.0)
            purifiers = [d for d in found
                         if "airpurifier" in str(d.get("info", {})).lower()]
            pick = purifiers[0] if purifiers else (found[0] if len(found) == 1 else None)
            if pick is None:
                raise SystemExit(
                    "没有自动发现净化器。请确认净化器已通电并连上同一台路由器的 Wi-Fi，\n"
                    "然后用 `python bridge.py discover` 手工确认，或从米家云端提取 token 填进 config.json。"
                )
            p["ip"], p["token"] = pick["ip"], pick["token"]
            save_config(self.cfg)
            LOG.info("已自动写入 %s / token=%s…", p["ip"], p["token"][:6])
        self.device = miio_lan.MiioDevice(
            p["ip"], p["token"], timeout=float(p.get("timeout_s", 4)))
        self._learn_identity()

    # -- 断电/换 IP 后的自愈 ----------------------------------------------
    def _learn_identity(self) -> None:
        """记下 MAC/型号，断电换 IP 后靠它认人。"""
        if self.device is None:
            return
        try:
            info = self.device.info()
        except Exception as ex:
            LOG.debug("取设备信息失败：%s", ex)
            return
        p = self.cfg["purifier"]
        changed = False
        mac = str(info.get("mac", "")).lower()
        if mac and mac != str(p.get("mac", "")).lower():
            p["mac"] = mac
            self.mac = mac
            changed = True
        if info.get("model") and info["model"] != p.get("model"):
            p["model"] = info["model"]
            changed = True
        if changed:
            save_config(self.cfg, self.cfg_path)
            LOG.info("设备身份：model=%s mac=%s", p.get("model"), p.get("mac"))

    def _silence_buzzer(self) -> None:
        """切档前把提示音关掉，避免每次调档都"滴"。

        原状态记在 config 的 purifier.buzzer_was 里，这样即使关机/断电前没来得及
        交还，下次运行结束时也会恢复。
        """
        if not self.silent or self._silenced or self.device is None:
            return
        try:
            cur = self.device.status().get("buzzer")
            if cur == "off":
                if self.cfg["purifier"].get("buzzer_was") == "on":
                    LOG.info("提示音仍是上次关掉的状态，交还时会恢复")
                self._silenced = True
                return
            self.device.set_buzzer(False)
            self.cfg["purifier"]["buzzer_was"] = cur or "on"
            save_config(self.cfg, self.cfg_path)
            LOG.info("已关闭净化器提示音（交还时恢复为 %s）", cur or "on")
            self._silenced = True
        except miio_lan.MiioError as ex:
            LOG.debug("关提示音失败：%s", ex)

    def _restore_buzzer(self) -> None:
        if self.device is None:
            return
        was = self.cfg["purifier"].pop("buzzer_was", None)
        if was is None:
            self._silenced = False
            return
        if was == "on":
            try:
                self.device.set_buzzer(True)
                LOG.info("已恢复净化器提示音")
            except miio_lan.MiioError as ex:
                LOG.debug("恢复提示音失败：%s", ex)
        save_config(self.cfg, self.cfg_path)
        self._silenced = False

    def _on_device_error(self, ex: Exception) -> None:
        self.fail_count += 1
        delay = min(300.0, 10.0 * (2 ** min(self.fail_count, 5)))
        self.next_retry = time.time() + delay
        LOG.warning("设备通信失败（第 %d 次），%.0fs 后重试：%s",
                    self.fail_count, delay, ex)
        if self.fail_count >= 2:
            self._rediscover()

    def _rediscover(self) -> bool:
        """断电重启后净化器可能换了 IP，广播找回来（按 MAC 认）。"""
        LOG.info("尝试重新发现净化器……")
        last_ip = str(self.cfg["purifier"].get("ip", ""))
        subnets = []
        if last_ip.count(".") == 3:
            subnets.append(last_ip.rsplit(".", 1)[0] + ".")
        try:
            found = miio_lan.discover(timeout=4.0, with_info=True, subnets=subnets)
        except Exception as ex:
            LOG.warning("重新发现失败：%s", ex)
            return False

        cand = None
        for dev in found:
            info = dev.get("info") or {}
            if "airpurifier" not in str(info.get("model", "")).lower():
                continue
            if self.mac and str(info.get("mac", "")).lower() == self.mac:
                cand = dev
                break
            cand = cand or dev
        if cand is None:
            LOG.warning("重新发现：局域网里没有空气净化器")
            return False

        p = self.cfg["purifier"]
        if cand["ip"] == p.get("ip") and cand["token"] == p.get("token"):
            LOG.info("重新发现：地址没变（%s）", cand["ip"])
            return False

        LOG.info("净化器地址更新：%s → %s", p.get("ip"), cand["ip"])
        p["ip"], p["token"] = cand["ip"], cand["token"]
        info = cand.get("info") or {}
        if info.get("mac"):
            p["mac"] = str(info["mac"]).lower()
            self.mac = p["mac"]
        if info.get("model"):
            p["model"] = info["model"]
        save_config(self.cfg, self.cfg_path)

        self.device = miio_lan.MiioDevice(
            p["ip"], p["token"], timeout=float(p.get("timeout_s", 4)))
        self.last_level = None
        self.fail_count = 0
        self.next_retry = 0.0
        return True

    def status(self) -> Dict[str, Any]:
        if self.device is None:
            raise SystemExit("还没有可用的设备：先跑 `python bridge.py discover`，"
                             "或把 ip/token 填进 config.json")
        return self.device.status()

    # -- 采样 -------------------------------------------------------------
    def sample(self) -> List[Tuple[str, str, float, float]]:
        """返回 [(传感器名, 实际测点, 平滑值, 目标档位)]。"""
        out: List[Tuple[str, str, float, float]] = []
        for spec in self.cfg["sensors"]:
            name = spec.get("name") or spec["kind"]
            if time.time() < self._fail_until.get(name, 0):
                continue  # 刚失败过，别每轮都白试（尤其 WMI 调用要 0.5s）
            try:
                sample = sampler.read(spec)
            except Exception as ex:
                self._fail_until[name] = time.time() + 60
                LOG.warning("传感器 %s(%s) 读取失败：%s", name, spec.get("kind"), ex)
                continue
            self._fail_until.pop(name, None)
            alpha = float(spec.get("ema_alpha", self.control.get("ema_alpha", 0.35)))
            prev = self.ema.get(name)
            smoothed = sample.value if prev is None else prev + alpha * (sample.value - prev)
            self.ema[name] = smoothed
            out.append((name, sample.source, smoothed,
                        curve_value(sampler.curve_for(spec, sample), smoothed)))
        return out

    # -- 执行 -------------------------------------------------------------
    def _set_level(self, level: int) -> None:
        """设置最爱档位；主指令失败时自动换另一种指令。"""
        try:
            self.device.set_favorite_level(level, self.level_command)
        except miio_lan.MiioError as ex:
            alt = "set_level" if self.level_command == "set_level_favorite" else "set_level_favorite"
            LOG.warning("%s 失败（%s），换 %s 再试", self.level_command, ex, alt)
            self.device.set_favorite_level(level, alt)
            self.level_command = alt
            self.cfg["purifier"]["level_command"] = self.level_command

    def _push_level(self, level: int) -> None:
        if self.device is None:
            raise SystemExit("没有可用设备，无法下发档位")
        self._silence_buzzer()
        try:
            st = self.device.status()
        except miio_lan.MiioError as ex:
            LOG.error("读设备状态失败：%s", ex)
            raise
        if st.get("power") != "on":
            LOG.info("净化器处于关机状态，先开机")
            self.device.set_power(True)
        if st.get("mode") != "favorite":
            # 关键：切到「最爱」时设备会恢复它记忆的档位（可能是 8/10 档）。
            # 先在 auto 模式下把记忆档位写成目标值，再切模式，就不会先狂吹几秒。
            try:
                self._set_level(level)
            except miio_lan.MiioError as ex:
                LOG.debug("切模式前预写档位失败（不影响后续）：%s", ex)
            LOG.info("切到最爱模式（当前 %s）", st.get("mode"))
            self.device.set_mode("favorite")
        self._set_level(level)
        self.last_cmd_ts = time.time()
        self.last_level = level
        self._released = False
        self.last["pushed"] = level

    def _in_active_hours(self) -> bool:
        windows = self.control.get("active_hours") or []
        if not windows:
            return True
        now = time.localtime()
        minutes = now.tm_hour * 60 + now.tm_min
        for win in windows:
            try:
                start_s, end_s = str(win).split("-", 1)
                sh, sm = (int(x) for x in start_s.strip().split(":"))
                eh, em = (int(x) for x in end_s.strip().split(":"))
            except ValueError:
                LOG.warning("active_hours 格式应为 'HH:MM-HH:MM'：%r", win)
                continue
            start, end = sh * 60 + sm, eh * 60 + em
            if start <= end:
                if start <= minutes < end:
                    return True
            elif minutes >= start or minutes < end:  # 跨午夜
                return True
        return False

    def _release(self, reason: str) -> None:
        """把设备交还给它自己（自动模式 / 关机），同一状态只做一次。"""
        self.idle_since = None
        self.last_level = None
        if self._released:
            return
        action = str(self.control.get("idle_action", "auto"))
        self._released = True
        if action in ("keep", "none", ""):
            LOG.info("%s；idle_action=keep，保持不动", reason)
            return
        if self.dry_run or self.device is None:
            LOG.info("[dry-run] %s；本应交还 %s", reason, action)
            return
        try:
            if action == "off":
                self.device.set_power(False)
                LOG.info("%s；已关机", reason)
            else:
                self.device.set_power(True)
                self.device.set_mode("auto")
                LOG.info("%s；已切回自动模式", reason)
            self._restore_buzzer()
        except miio_lan.MiioError as ex:
            LOG.error("交还设备失败：%s", ex)
            self._released = False

    def step(self) -> Optional[int]:
        if time.time() < self.next_retry:
            return self.last_level
        if not self._in_active_hours():
            LOG.info("不在 active_hours 时段内，本轮不动设备")
            self._release("不在生效时段")
            return self.last_level

        samples = self.sample()
        if not samples:
            LOG.warning("所有传感器都不可用，本轮回退：保持当前档位不动")
            return self.last_level

        raw_level = max(s[3] for s in samples)
        lo, hi = int(self.control["min_level"]), int(self.control["max_level"])
        target = int(round(min(max(raw_level, lo), hi)))

        detail = "  ".join("%s=%s(%s→%.1f档)" % (n, mn, format_value(t, mn), l)
                           for n, mn, t, l in samples)
        LOG.info("采样 %s → 目标 %d 档", detail, target)
        self.last = {"ts": time.time(),
                     "samples": [{"name": n, "source": mn, "temp": round(t, 1),
                                  "curve_level": round(l, 2)} for n, mn, t, l in samples],
                     "target": target}

        if self.last_level is None:
            if self.device is None:
                self.last_level = 0
            else:
                try:
                    st = self.status()
                    self.last_level = int(st.get("favorite_level") or 0)
                    LOG.info("设备当前：power=%s mode=%s level=%s",
                             st.get("power"), st.get("mode"), self.last_level)
                except miio_lan.MiioError as ex:
                    self._on_device_error(ex)
                    LOG.warning("读不到设备状态，先按保持处理")
                    return self.last_level

        # 低于曲线下限：进入待机计时
        if target <= lo:
            if self.idle_since is None:
                self.idle_since = time.time()
            idle_min = float(self.control.get("idle_release_min", 0) or 0)
            if idle_min > 0 and (time.time() - self.idle_since) >= idle_min * 60:
                self._release("温度长时间低于曲线下限")
                return self.last_level
        else:
            self.idle_since = None

        # 回差：变化小于 deadband 就不折腾设备
        deadband = int(self.control.get("deadband", 1))
        if abs(target - self.last_level) < max(1, deadband):
            return self.last_level

        # 两条指令之间至少隔 min_command_gap_s 秒
        gap = float(self.control.get("min_command_gap_s", 0) or 0)
        if (time.time() - self.last_cmd_ts) < gap:
            LOG.debug("距上次下发不足 %.0fs，跳过", gap)
            return self.last_level

        if self.dry_run:
            LOG.info("[dry-run] 本应下发 %d 档", target)
            self.last_cmd_ts = time.time()
            self.last_level = target
            return target
        try:
            self._push_level(target)
            self.fail_count = 0
            self.next_retry = 0.0
            LOG.info("已下发 %d 档", target)
        except miio_lan.MiioError as ex:
            self._on_device_error(ex)
        return self.last_level

    def run(self) -> None:
        interval = max(2.0, float(self.control.get("interval_s", 10)))
        LOG.info("开始闭环控制，周期 %.0fs，Ctrl-C 退出", interval)
        try:
            while True:
                try:
                    self.step()
                except KeyboardInterrupt:
                    raise
                except Exception as ex:  # 单轮异常不退出常驻进程
                    LOG.exception("本轮异常：%s", ex)
                time.sleep(interval)
        finally:
            if self.control.get("restore_on_exit", True):
                try:
                    self._release("程序退出")
                except Exception as ex:
                    LOG.error("退出时交还设备失败：%s", ex)


# --------------------------------------------------------------------------
# 命令行
# --------------------------------------------------------------------------
def setup_logging(verbose: bool = False) -> None:
    os.makedirs(LOG_DIR, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S")
    LOG.setLevel(logging.DEBUG if verbose else logging.INFO)
    LOG.handlers.clear()
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    LOG.addHandler(sh)
    fh = RotatingFileHandler(os.path.join(LOG_DIR, "bridge.log"),
                             maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
    LOG.addHandler(fh)


def cmd_discover(cfg: Dict[str, Any]) -> int:
    LOG.info("广播搜索 miIO 设备（约 4 秒）……")
    found = miio_lan.discover(timeout=4.0)
    if not found:
        LOG.warning("没找到任何 miIO 设备。")
        LOG.warning("排查：①净化器通电且米家显示在线 ②和电脑连同一个路由器/同一网段 "
                    "③路由器没开「AP 隔离」④净化器在 2.4G，电脑换成同一个网段的连接")
        return 1
    for dev in found:
        info = dev.get("info") or {}
        LOG.info("IP=%-15s token=%s model=%s", dev["ip"], dev["token"],
                 info.get("model", "?"))
    if len(found) == 1:
        cfg["purifier"]["ip"] = found[0]["ip"]
        cfg["purifier"]["token"] = found[0]["token"]
        save_config(cfg)
        LOG.info("已写入 config.json")
    return 0


def cmd_probe(cfg: Dict[str, Any]) -> int:
    for spec in cfg["sensors"]:
        name = spec.get("name") or spec["kind"]
        try:
            readings = sensors.read(spec["kind"], host=spec.get("host", "127.0.0.1"),
                                    port=int(spec.get("port", 8085)))
            LOG.info("[%s:%s] 可用测点：", name, spec["kind"])
            for r in readings:
                mark = " <== 匹配" if sensors.pick([r], spec.get("match")) else ""
                print("    %-40s %6.1f%s" % (r.name, r.value, mark))
        except Exception as ex:
            LOG.warning("[%s:%s] 不可用：%s", name, spec["kind"], ex)
    return 0


def cmd_selftest() -> int:
    ok = True

    def check(label: str, cond: bool) -> None:
        nonlocal ok
        print(("  PASS  " if cond else "  FAIL  ") + label)
        ok = ok and cond

    print("1) AES-128 已知答案测试")
    key = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
    ct = miio_lan._encrypt_block(bytes.fromhex("00112233445566778899aabbccddeeff"),
                                 miio_lan._expand_key(key))
    check("FIPS-197 C.1 向量", ct.hex() == "69c4e0d86a7b0430d8cdb78070b4c55a")
    k2 = bytes.fromhex("2b7e151628aed2a6abf7158809cf4f3c")
    iv = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
    cbc = miio_lan.aes_cbc_encrypt(
        bytes.fromhex("6bc1bee22e409f96e93d7e117393172a"), k2, iv)
    check("NIST SP800-38A CBC 向量", cbc.hex() == "7649abac8119b246cee98e9b12e9197d")

    print("2) 折线插值")
    curve = [[45, 0], [55, 10]]
    check("端点", curve_value(curve, 45) == 0 and curve_value(curve, 55) == 10)
    check("中点", abs(curve_value(curve, 50) - 5) < 1e-9)
    check("越界钳制", curve_value(curve, 90) == 10 and curve_value(curve, 10) == 0)

    print("3) 与假净化器完整握手 / 读状态 / 设档")
    import fake_purifier
    token = "00112233445566778899aabbccddeeff"
    dev = fake_purifier.FakePurifier(token=token).start()
    try:
        client = miio_lan.MiioDevice("127.0.0.1", token, timeout=3)
        info = client.info()
        check("miIO.info 解出模型 %s" % info.get("model"), info.get("model") == fake_purifier.FAKE_MODEL)
        st = client.status()
        check("status 读到 mode=%s" % st.get("mode"), st.get("mode") == "auto")
        client.set_power(True)
        client.set_mode("favorite")
        client.set_favorite_level(13)
        check("设备侧 favorite_level=13", dev.state["favorite_level"] == 13)
        check("设备侧 mode=favorite", dev.state["mode"] == "favorite")
        check("设备侧 power=on", dev.state["power"] == "on")
        try:
            miio_lan.MiioDevice("127.0.0.1", "ff" * 16, timeout=2).status()
            check("错误 token 必须失败", False)
        except miio_lan.MiioError:
            check("错误 token 被拒绝", True)
    finally:
        dev.stop()

    print("4) LibreHardwareMonitor Web 数据解析（离线桩）")
    import http.server
    import json as _json
    import threading

    sample = {
        "id": 0, "Text": "Sensor",
        "Children": [
            {"id": "/intelcpu/0", "Text": "12th Gen", "Children": [
                {"id": "/intelcpu/0/temperature/0", "Text": "CPU Package", "Value": "61.0 °C"},
                {"id": "/intelcpu/0/temperature/1", "Text": "Core #1", "Value": "58.5 °C"},
            ]},
            {"id": "/gpu-nvidia/0", "Text": "NVIDIA", "Children": [
                {"id": "/gpu-nvidia/0/temperature/0", "Text": "GPU Core", "Value": "55.0 °C"},
                {"id": "/gpu-nvidia/0/load/0", "Text": "GPU Core", "Value": "30.0 %"},
            ]},
        ],
    }

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = _json.dumps(sample).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # 静音
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 18085), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        readings = sensors.lhm_web(port=18085)
        check("只收集 °C 项（3 个）", len(readings) == 3)
        cpu = sensors.pick(readings, ["CPU Package"])
        check("按关键字匹配到 CPU Package=61.0",
              cpu is not None and abs(cpu.value - 61.0) < 1e-6)
    finally:
        srv.shutdown()

    print("\n结果：" + ("全部通过" if ok else "有失败项"))
    return 0 if ok else 1


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="笔记本温度 → 空气净化器风速")
    parser.add_argument("command", nargs="?", default="run",
                        choices=["run", "once", "status", "probe", "discover",
                                 "set", "selftest", "init"])
    parser.add_argument("value", nargs="?", type=int, help="set 用的档位")
    parser.add_argument("--config", default=CONFIG_PATH)
    parser.add_argument("--dry-run", action="store_true", help="只算不下发")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    setup_logging(args.verbose)

    if args.command == "selftest":
        return cmd_selftest()

    cfg = load_config(args.config)
    if args.command == "init":
        save_config(cfg, args.config)
        LOG.info("配置已写入 %s", args.config)
        return 0
    if args.command == "discover":
        return cmd_discover(cfg)
    if args.command == "probe":
        return cmd_probe(cfg)

    ctrl = Controller(cfg, dry_run=args.dry_run)

    if args.command == "status":
        st = ctrl.status()
        print(json.dumps(st, ensure_ascii=False, indent=2))
        return 0

    if args.command == "set":
        if args.value is None:
            parser.error("set 需要档位，例如：python bridge.py set 12")
        ctrl._push_level(max(0, min(17, args.value)))
        LOG.info("已设为 %d 档", args.value)
        return 0

    if args.command == "once":
        level = ctrl.step()
        LOG.info("本次结论：%s 档", level)
        return 0

    def _bye(signum, frame):  # noqa: ARG001
        LOG.info("收到退出信号，停止")
        raise SystemExit(0)

    signal.signal(signal.SIGINT, _bye)
    signal.signal(signal.SIGTERM, _bye)
    ctrl.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
