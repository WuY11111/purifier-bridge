#!/usr/bin/env python3
"""散热管家 —— 笔记本温度 × 小米空气净化器 的图形控制台。

双击 散热管家.exe（或 python app.py）打开窗口：
  · 实时看 CPU/GPU 温度、净化器档位与转速
  · 拖动曲线上的点，直接改「多少度给几档」
  · 开始/暂停控制、试档位、交还自动模式
  · 一键启动 LibreHardwareMonitor（需要 UAC）
  · 开机自启开关（静默后台运行，双击图标秒开窗口）

命令行：
  python app.py            打开窗口
  python app.py --silent   只跑后台控制，不弹窗（开机自启用）
"""

from __future__ import annotations

import ctypes
import json
import logging
import os
import queue
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
from logging.handlers import RotatingFileHandler
from tkinter import messagebox, ttk
from typing import Any, Dict, List, Optional

import bridge
import miio_lan
import sampler
import tray as tray_mod
from config import DEFAULT_CONFIG

# 打包成 exe 后 BASE_DIR 要指向 exe 所在目录（config/logs/tools 都在旁边），
# 而不是 PyInstaller 解压出来的临时目录
def _resolve_base_dir() -> str:
    """exe 所在目录；若其上级目录有 config.json（打包后放在项目里），就用上级。"""
    if getattr(sys, "frozen", False):
        here = os.path.dirname(os.path.abspath(sys.executable))
        parent = os.path.dirname(here)
        if os.path.exists(os.path.join(parent, "config.json")):
            return parent
        return here
    return os.path.dirname(os.path.abspath(__file__))


BASE_DIR = _resolve_base_dir()
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
LOG_DIR = os.path.join(BASE_DIR, "logs")
LHM_EXE = os.path.join(BASE_DIR, "tools", "LibreHardwareMonitor", "LibreHardwareMonitor.exe")
IPC_PORT = 8791
TASK_NAME = "PurifierBridgeApp"

BG = "#161a21"
CARD = "#1f2530"
CARD2 = "#252c39"
FG = "#e7ecf5"
MUTED = "#8b96a8"
ACCENT = "#4f8cff"
OK = "#39c07a"
WARN = "#f0b23a"
BAD = "#ef5b5b"

FONT = "Microsoft YaHei UI"

# 三套预设曲线（按 x=温度, y=档位）
PRESETS: Dict[str, Dict[str, List[List[int]]]] = {
    "静音": {
        "cpu": [[55, 0], [65, 2], [75, 4], [85, 6], [95, 9]],
        "gpu": [[50, 0], [58, 2], [66, 5], [74, 8], [82, 11]],
    },
    "均衡": {
        "cpu": [[52, 0], [60, 2], [68, 4], [76, 7], [83, 10], [88, 13], [94, 16]],
        "gpu": [[45, 0], [52, 2], [58, 4], [64, 7], [70, 10], [76, 13], [82, 16]],
    },
    "散热": {
        "cpu": [[45, 0], [55, 3], [62, 6], [68, 9], [75, 12], [82, 16]],
        "gpu": [[40, 0], [48, 3], [55, 6], [62, 9], [68, 12], [75, 16]],
    },
}
LOG = logging.getLogger("bridge")


# --------------------------------------------------------------------------
# 资源路径（兼容 PyInstaller 打包）
# --------------------------------------------------------------------------
def resource_path(*parts: str) -> str:
    base = getattr(sys, "_MEIPASS", BASE_DIR)
    return os.path.join(base, *parts)


# --------------------------------------------------------------------------
# 日志环形缓冲，给界面用
# --------------------------------------------------------------------------
class RingHandler(logging.Handler):
    def __init__(self, cap: int = 300) -> None:
        super().__init__()
        self.lines: List[str] = []
        self.cap = cap
        self._buf_lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
        except Exception:
            return
        with self._buf_lock:
            self.lines.append(msg)
            if len(self.lines) > self.cap:
                del self.lines[: len(self.lines) - self.cap]

    def snapshot(self, n: int = 200) -> List[str]:
        with self._buf_lock:
            return self.lines[-n:]


def trace(msg: str) -> None:
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(os.path.join(LOG_DIR, "trace.txt"), "a", encoding="utf-8") as fh:
            fh.write("%s %s\n" % (time.strftime("%H:%M:%S"), msg))
    except Exception:
        pass


RING = RingHandler()


STATE_PATH = os.path.join(BASE_DIR, "state.json")


def load_state() -> Dict[str, Any]:
    try:
        with open(STATE_PATH, "r", encoding="utf-8-sig") as fh:
            return json.load(fh)
    except Exception:
        return {}


def save_state(**kw: Any) -> None:
    data = load_state()
    data.update(kw)
    try:
        with open(STATE_PATH, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
    except Exception:
        pass


# --------------------------------------------------------------------------
# 控制引擎（后台线程）
# --------------------------------------------------------------------------
class Engine:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.cfg: Dict[str, Any] = bridge.load_config(CONFIG_PATH)
        self.ctrl: Optional[bridge.Controller] = None
        self.ctrl_error = ""
        self.control_on = False
        self.temps: Dict[str, Dict[str, Any]] = {}
        self.device: Dict[str, Any] = {}
        self.last_error = ""
        self.started_at = time.time()
        self._err_logged: Dict[str, float] = {}
        self._last_wifi_try = 0.0
        self._stop = threading.Event()
        trace('Engine: rebuild start')
        self._rebuild()
        trace('Engine: rebuild ok')
        LOG.info("引擎启动：设备 %s", (self.cfg["purifier"].get("ip") or "未配置"))
        if self.ctrl is None:
            LOG.warning("控制器未就绪：%s", self.ctrl_error)
        trace('Engine: spawn sampler')
        self._spawn(self._sample_loop)
        trace('Engine: spawn device')
        self._spawn(self._device_loop)
        trace('Engine: spawn control')
        self._spawn(self._control_loop)
        self.refresh_device()
        self._ensure_network()
        trace('Engine: done')

    # -- 基础设施 ---------------------------------------------------------
    def _spawn(self, target) -> None:
        t = threading.Thread(target=target, daemon=True)
        t.start()

    def _rebuild(self) -> None:
        self.control_on = False
        time.sleep(0.05)
        try:
            self.ctrl = bridge.Controller(self.cfg, dry_run=False, config_path=CONFIG_PATH)
            self.ctrl_error = ""
        except SystemExit as ex:
            self.ctrl = None
            self.ctrl_error = str(ex)
        except Exception as ex:
            self.ctrl = None
            self.ctrl_error = "初始化失败：%s" % ex

    def apply_config(self, cfg: Dict[str, Any]) -> None:
        with self.lock:
            was_on = self.control_on
            self.cfg = cfg
            bridge.save_config(cfg, CONFIG_PATH)
            self._rebuild()
            self.control_on = was_on

    # -- 采样（与控制器无关，界面刷新用）-----------------------------------
    def _sample_loop(self) -> None:
        while not self._stop.is_set():
            self._sample_once()
            self._stop.wait(2.5)

    def _sample_once(self) -> None:
        out: Dict[str, Dict[str, Any]] = {}
        for spec in self.cfg["sensors"]:
            name = spec.get("name") or spec["kind"]
            try:
                sample = sampler.read(spec)
            except Exception as ex:
                out[name] = {"error": str(ex)[:200]}
                if time.time() - self._err_logged.get(name, 0) > 60:
                    self._err_logged[name] = time.time()
                    LOG.warning("%s 读数失败：%s", name, str(ex)[:140])
                continue
            out[name] = {
                "source": sample.source,
                "value": sample.value,
                "level": bridge.curve_value(sampler.curve_for(spec, sample), sample.value),
                "fallback": sample.fallback,
                "ts": time.time(),
            }
        with self.lock:
            self.temps = out

    # -- 设备状态 ---------------------------------------------------------
    def _device_loop(self) -> None:
        while not self._stop.is_set():
            # 连不上时放慢轮询，断电/路由器重启期间别刷屏
            connected = bool(self.device.get("connected"))
            self._stop.wait(3.0 if connected else 20.0)
            if self.control_on:
                continue  # 控制线程自己会读，避免抢同一个 UDP 会话
            before = bool(self.device.get("connected"))
            self.refresh_device()
            if not before and not self.device.get("connected"):
                self._maybe_connect_wifi()

    def refresh_device(self) -> None:
        ctrl = self.ctrl
        if ctrl is None or ctrl.device is None:
            with self.lock:
                self.device = {}
            return
        try:
            st = ctrl.device.status()
            with self.lock:
                self.device = dict(st)
                self.device["connected"] = True
            # 网络恢复了，立刻解除控制器的退避
            ctrl.next_retry = 0.0
            ctrl.fail_count = 0
        except Exception as ex:
            with self.lock:
                self.device = {"connected": False, "error": str(ex)[:120]}

    # -- 断网自愈 ---------------------------------------------------------
    def _ensure_network(self) -> None:
        """开机时若净化器网段不挂在任何网卡上，立刻把 Wi-Fi 连上，不用等重试。"""
        ip = str(self.cfg["purifier"].get("ip") or "").strip()
        if ip.count(".") != 3:
            return
        prefix = ip.rsplit(".", 1)[0] + "."
        try:
            local = miio_lan.local_subnets()
        except Exception:
            return
        if prefix in local:
            return
        LOG.info("净化器在 %s 网段，本机网卡上没有 → 立刻尝试连接 Wi-Fi", prefix)
        self._maybe_connect_wifi()

    def _maybe_connect_wifi(self) -> None:
        """设备连不上、且配置里指定了 Wi-Fi 配置名时，自动重连一次。"""
        profile = str(self.cfg["purifier"].get("wifi_profile") or "").strip()
        if not profile:
            return
        now = time.time()
        if now - self._last_wifi_try < 90:
            return
        self._last_wifi_try = now
        script = os.path.join(BASE_DIR, "tools", "wifi-connect.ps1")
        if not os.path.exists(script):
            LOG.debug("没有 wifi-connect.ps1，跳过自动连 Wi-Fi")
            return
        LOG.info("设备连不上，尝试自动连接 Wi-Fi「%s」……", profile)
        try:
            subprocess.Popen(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                 "-File", script, "-Profile", profile],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=0x08000000)
        except Exception as ex:
            LOG.warning("自动连 Wi-Fi 失败：%s", ex)

    # -- 控制 -------------------------------------------------------------
    def _control_loop(self) -> None:
        while not self._stop.is_set():
            if not self.control_on:
                self._stop.wait(0.5)
                continue
            ctrl = self.ctrl
            if ctrl is None:
                self.control_on = False
                continue
            try:
                ctrl.step()
                self.last_error = ""
                self.refresh_device_locked(ctrl)
            except SystemExit as ex:
                self.last_error = str(ex)
                self.control_on = False
                LOG.error("控制停止：%s", ex)
            except Exception as ex:
                self.last_error = str(ex)
                LOG.error("本轮异常：%s", ex)
            if not self.device.get("connected"):
                self._maybe_connect_wifi()
            interval = max(2.0, float(self.cfg["control"].get("interval_s", 10)))
            self._stop.wait(interval)

    def refresh_device_locked(self, ctrl: bridge.Controller) -> None:
        try:
            st = ctrl.device.status() if ctrl.device else {}
            with self.lock:
                self.device = dict(st)
                self.device["connected"] = True
            ctrl.next_retry = 0.0
            ctrl.fail_count = 0
        except Exception as ex:
            with self.lock:
                self.device = {"connected": False, "error": str(ex)[:120]}

    # -- 对外动作 ---------------------------------------------------------
    def start_control(self) -> None:
        if self.ctrl is None:
            self._rebuild()
        if self.ctrl is None:
            raise RuntimeError(self.ctrl_error or "没有可用的净化器配置")
        # 先把设备接管过来（开机 + 切最爱模式 + 用当前档位），否则它会一直待在 auto
        try:
            st = self.ctrl.device.status() if self.ctrl.device else {}
            level = int(st.get("favorite_level") or 0)
            self.ctrl._push_level(level)
            self.refresh_device_locked(self.ctrl)
        except Exception as ex:
            LOG.warning("接管设备失败（继续尝试控制）：%s", ex)
        self.control_on = True
        save_state(running=True)
        LOG.info("开始闭环控制")

    def stop_control(self) -> None:
        self.control_on = False
        save_state(running=False)
        LOG.info("控制已暂停（净化器保持当前状态）")

    def release(self) -> None:
        self.control_on = False
        save_state(running=False)
        ctrl = self.ctrl
        if ctrl is None or ctrl.device is None:
            return
        try:
            ctrl.device.set_power(True)
            ctrl.device.set_mode("auto")
            LOG.info("已交还自动模式")
        except Exception as ex:
            LOG.error("交还失败：%s", ex)
            raise

    def try_level(self, level: int) -> None:
        ctrl = self.ctrl
        if ctrl is None or ctrl.device is None:
            raise RuntimeError("没有可用的净化器")
        ctrl._push_level(int(level))
        LOG.info("试了下 %d 档", level)

    def shutdown(self) -> None:
        self.control_on = False
        if self.cfg["control"].get("restore_on_exit", True):
            try:
                self.release()
            except Exception:
                pass
        self._stop.set()


# --------------------------------------------------------------------------
# 曲线编辑器
# --------------------------------------------------------------------------
class CurveEditor(tk.Canvas):
    XPAD = 46
    YPAD = 22
    T_MIN, T_MAX = 30.0, 105.0
    L_MIN, L_MAX = 0.0, 17.0

    def __init__(self, master, points: List[List[float]], on_change, **kw) -> None:
        super().__init__(master, bg=CARD2, highlightthickness=0, **kw)
        self.points = [[float(a), float(b)] for a, b in points]
        self.on_change = on_change
        self.drag_idx: Optional[int] = None
        self.marker: Optional[float] = None
        self.bind("<Configure>", lambda e: self.redraw())
        self.bind("<Button-1>", self._down)
        self.bind("<B1-Motion>", self._drag)
        self.bind("<ButtonRelease-1>", self._up)
        self.bind("<Button-3>", self._right)

    # -- 坐标变换 ---------------------------------------------------------
    def cw(self) -> int:
        return max(120, self.winfo_width())

    def ch(self) -> int:
        return max(80, self.winfo_height())

    def px_temp(self, t: float) -> float:
        span = self.T_MAX - self.T_MIN
        return self.XPAD + (t - self.T_MIN) / span * (self.cw() - self.XPAD - 12)

    def px_level(self, lv: float) -> float:
        span = self.L_MAX - self.L_MIN
        return self.ch() - self.YPAD - (lv - self.L_MIN) / span * (self.ch() - self.YPAD - 12)

    def temp_at(self, px: float) -> float:
        span = self.T_MAX - self.T_MIN
        return self.T_MIN + (px - self.XPAD) / max(1, self.cw() - self.XPAD - 12) * span

    def level_at(self, py: float) -> float:
        span = self.L_MAX - self.L_MIN
        return self.L_MIN + (self.ch() - self.YPAD - py) / max(1, self.ch() - self.YPAD - 12) * span

    # -- 绘制 -------------------------------------------------------------
    def redraw(self) -> None:
        self.delete("all")
        w, h = self.cw(), self.ch()
        # 网格
        for t in range(int(self.T_MIN) // 10 * 10, int(self.T_MAX) + 1, 10):
            x = self.px_temp(t)
            if x < self.XPAD - 1:
                continue
            self.create_line(x, 8, x, h - self.YPAD, fill="#2f3746")
            self.create_text(x, h - 10, text=str(t), fill=MUTED, font=(FONT, 8))
        for lv in range(0, 18, 2):
            y = self.px_level(lv)
            self.create_line(self.XPAD, y, w - 8, y, fill="#2f3746")
            self.create_text(self.XPAD - 12, y, text=str(lv), fill=MUTED, font=(FONT, 8))

        pts = sorted(self.points)
        coords: List[float] = []
        if pts:
            coords += [self.px_temp(pts[0][0]), self.px_level(pts[0][1])]
            for t, lv in pts[1:]:
                coords += [self.px_temp(t), self.px_level(lv)]
            self.create_line(*coords, fill=ACCENT, width=2, smooth=False)
            self.create_polygon(
                *coords, self.px_temp(pts[-1][0]), self.px_level(self.L_MIN),
                self.px_temp(pts[0][0]), self.px_level(self.L_MIN),
                fill="#2b3d5e", outline="",
            )
            self.create_line(*coords, fill=ACCENT, width=2)

        if self.marker is not None:
            x = self.px_temp(min(max(self.marker, self.T_MIN), self.T_MAX))
            self.create_line(x, 8, x, h - self.YPAD, fill="#ffb84d", dash=(4, 3))

        for i, (t, lv) in enumerate(pts):
            x, y = self.px_temp(t), self.px_level(lv)
            self.create_oval(x - 5, y - 5, x + 5, y + 5, fill=FG, outline=ACCENT, width=2,
                             tags=("pt", str(i)))
        if self.marker is not None:
            lv = bridge.curve_value(pts, self.marker) if pts else 0
            self.create_text(w - 10, 14, anchor="e",
                             text="%.0f℃ → %.1f 档" % (self.marker, lv),
                             fill="#ffb84d", font=(FONT, 9, "bold"))

    def set_marker(self, temp: Optional[float]) -> None:
        self.marker = temp
        self.redraw()

    # -- 交互 -------------------------------------------------------------
    def _hit(self, px: float, py: float) -> Optional[int]:
        pts = sorted(self.points)
        for i, (t, lv) in enumerate(pts):
            if abs(self.px_temp(t) - px) < 10 and abs(self.px_level(lv) - py) < 10:
                return self.points.index([t, lv])
        return None

    def _down(self, event) -> None:
        idx = self._hit(event.x, event.y)
        self.drag_idx = idx
        if idx is not None:
            self.redraw()

    def _drag(self, event) -> None:
        if self.drag_idx is None:
            return
        t = min(max(self.temp_at(event.x), self.T_MIN), self.T_MAX)
        lv = min(max(self.level_at(event.y), self.L_MIN), self.L_MAX)
        self.points[self.drag_idx] = [round(t), round(lv)]
        self.redraw()

    def _up(self, event) -> None:
        if self.drag_idx is not None:
            self.drag_idx = None
            self._normalize()
            self.on_change(self.export())

    def _right(self, event) -> None:
        idx = self._hit(event.x, event.y)
        if idx is not None and len(self.points) > 2:
            self.points.pop(idx)
            self._normalize()
            self.redraw()
            self.on_change(self.export())

    def _normalize(self) -> None:
        # 同温度的点合并，保持温度唯一升序
        merged: Dict[float, float] = {}
        for t, lv in self.points:
            merged[float(t)] = float(lv)
        self.points = [[t, lv] for t, lv in sorted(merged.items())]

    def export(self) -> List[List[float]]:
        return [[int(t), int(lv)] for t, lv in sorted(self.points)]

    def set_points(self, points: List[List[float]]) -> None:
        self.points = [[float(a), float(b)] for a, b in points]
        self.redraw()


# --------------------------------------------------------------------------
# 主窗口
# --------------------------------------------------------------------------
class App(tk.Tk):
    def __init__(self, silent: bool = False) -> None:
        trace('App: Tk created')
        super().__init__()
        trace('App: engine start')
        self.engine = Engine()
        trace('App: engine ok')
        LOG.info("窗口已创建")
        self.silent = silent
        self.curve_target = "cpu"
        self._closing = False
        self._tray_q: "queue.Queue[int]" = queue.Queue()
        self._tick_n = 0
        self.tray = None
        self._off_since = 0.0
        self._off_notified = False

        self.title("散热管家 · 小米空气净化器 × 笔记本")
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        w, h = 1120, min(860, max(700, sh - 110))
        self.geometry("%dx%d+%d+%d" % (w, h, max(0, (sw - w) // 2), max(0, (sh - h) // 3)))
        self.minsize(980, 700)
        self.configure(bg=BG)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(250, self._dark_titlebar)

        self._build_styles()
        trace('App: ui start')
        self._build_ui()
        trace('App: ui ok')
        self._refresh_curve()
        self._sync_settings()
        self._init_tray()
        if load_state().get("running"):
            try:
                self.engine.start_control()
                LOG.info("按上次状态自动开始控制")
            except Exception as ex:
                LOG.warning("自动开始控制失败：%s", ex)
        if silent:
            self.withdraw()
        self.after(300, self._tick)
        trace('App: ipc start')
        self._start_ipc()
        trace('App: ipc ok')
        LOG.info("界面构建完成")

    # -- 样式 -------------------------------------------------------------
    def _dark_titlebar(self) -> None:
        """Windows 11 深色标题栏，免得白边突兀。"""
        try:
            hwnd = ctypes.windll.user32.GetParent(self.winfo_id())
            flag = ctypes.c_int(1)
            for attr in (20, 19):
                if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                        hwnd, attr, ctypes.byref(flag), ctypes.sizeof(flag)) == 0:
                    break
        except Exception:
            pass

    def _build_styles(self) -> None:
        st = ttk.Style(self)
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        st.configure("TCombobox", fieldbackground=CARD2, background=CARD2,
                     foreground=FG, arrowcolor=FG, bordercolor="#39414f")
        st.map("TCombobox", fieldbackground=[("readonly", CARD2)],
               foreground=[("readonly", FG)])

    def _card(self, parent, **kw) -> tk.Frame:
        f = tk.Frame(parent, bg=CARD, highlightbackground="#2b3341",
                     highlightthickness=1, **kw)
        return f

    def _label(self, parent, text: str, size: int = 10, color: str = FG,
               bold: bool = False, bg: str = CARD) -> tk.Label:
        return tk.Label(parent, text=text, bg=bg, fg=color,
                        font=(FONT, size, "bold" if bold else "normal"),
                        anchor="w", justify="left")

    # -- 界面 -------------------------------------------------------------
    def _build_ui(self) -> None:
        root = tk.Frame(self, bg=BG)
        root.pack(fill="both", expand=True, padx=14, pady=12)

        # 顶栏
        head = tk.Frame(root, bg=BG)
        head.pack(fill="x")
        tk.Label(head, text="散热管家", bg=BG, fg=FG,
                 font=(FONT, 18, "bold")).pack(side="left")
        tk.Label(head, text="  笔记本温度 → 小米空气净化器 2 风速", bg=BG, fg=MUTED,
                 font=(FONT, 10)).pack(side="left", pady=(6, 0))
        self.pill_dev = self._pill(head, "净化器 …")
        self.pill_lhm = self._pill(head, "LHM …")
        self.pill_run = self._pill(head, "已暂停")

        # 先占住底部日志区，剩下的空间再给主体，避免窗口变矮时日志被挤出去
        self._build_log(root)

        body = tk.Frame(root, bg=BG)
        body.pack(fill="both", expand=True, pady=(10, 0))
        body.grid_columnconfigure(0, weight=3, uniform="c")
        body.grid_columnconfigure(1, weight=4, uniform="c")
        body.grid_rowconfigure(0, weight=1)

        left = tk.Frame(body, bg=BG)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        right = tk.Frame(body, bg=BG)
        right.grid(row=0, column=1, sticky="nsew")

        self._build_live(left)
        self._build_settings(left)
        self._build_system(left)
        self._build_curve(right)

    def _pill(self, parent, text: str) -> tk.Label:
        lb = tk.Label(parent, text="● " + text, bg=CARD2, fg=MUTED,
                      font=(FONT, 9), padx=10, pady=4)
        lb.pack(side="right", padx=(8, 0))
        return lb

    def _build_live(self, parent) -> None:
        card = self._card(parent)
        card.pack(fill="x")
        inner = tk.Frame(card, bg=CARD)
        inner.pack(fill="x", padx=16, pady=14)
        inner.grid_columnconfigure(0, weight=1)
        inner.grid_columnconfigure(1, weight=1)

        self.lbl_cpu_box, self.lbl_cpu = self._big(inner, "CPU")
        self.lbl_cpu_box.grid(row=0, column=0, sticky="w")
        self.lbl_gpu_box, self.lbl_gpu = self._big(inner, "GPU")
        self.lbl_gpu_box.grid(row=0, column=1, sticky="w")

        self.lbl_level = tk.Label(inner, text="当前 —", bg=CARD, fg=FG,
                                  font=(FONT, 13, "bold"), anchor="w")
        self.lbl_level.grid(row=1, column=0, columnspan=2, sticky="w", pady=(10, 0))
        self.lbl_extra = self._label(inner, "AQI — · —", 9, MUTED)
        self.lbl_extra.grid(row=2, column=0, columnspan=2, sticky="w", pady=(2, 0))
        self.lbl_target = self._label(inner, "目标 —", 9, MUTED)
        self.lbl_target.grid(row=3, column=0, columnspan=2, sticky="w")

    def _big(self, parent, title: str) -> "tuple[tk.Frame, tk.Label]":
        box = tk.Frame(parent, bg=CARD)
        self._label(box, title, 9, MUTED).pack(anchor="w")
        lb = tk.Label(box, text="—", bg=CARD, fg=FG, font=(FONT, 30, "bold"), anchor="w")
        lb.pack(anchor="w")
        return box, lb

    def _build_settings(self, parent) -> None:
        card = self._card(parent)
        card.pack(fill="x", pady=(10, 0))
        inner = tk.Frame(card, bg=CARD)
        inner.pack(fill="x", padx=16, pady=12)
        self._label(inner, "参数", 11, FG, True).grid(row=0, column=0, columnspan=4,
                                                     sticky="w", pady=(0, 8))

        def field(row: int, col: int, text: str, width: int = 7) -> tk.Entry:
            self._label(inner, text, 9, MUTED).grid(row=row, column=col * 2, sticky="w",
                                                    pady=3, padx=(0, 6))
            e = tk.Entry(inner, width=width, bg=CARD2, fg=FG, insertbackground=FG,
                         relief="flat", font=(FONT, 10))
            e.grid(row=row, column=col * 2 + 1, sticky="w", pady=3, padx=(0, 18))
            return e

        self.e_interval = field(1, 0, "采样周期(s)")
        self.e_gap = field(1, 1, "最小间隔(s)")
        self.e_min = field(2, 0, "最低档")
        self.e_max = field(2, 1, "最高档")
        self.e_dead = field(3, 0, "回差(档)")
        self.e_idle = field(3, 1, "空闲交还(min)")

        self._label(inner, "生效时段", 9, MUTED).grid(row=4, column=0, sticky="w", pady=3)
        self.e_hours = tk.Entry(inner, width=22, bg=CARD2, fg=FG, insertbackground=FG,
                                relief="flat", font=(FONT, 10))
        self.e_hours.grid(row=4, column=1, columnspan=2, sticky="w", pady=3)

        self._label(inner, "交还方式", 9, MUTED).grid(row=5, column=0, sticky="w", pady=3)
        self.cb_idle = ttk.Combobox(inner, values=["auto", "off", "keep"], width=8,
                                    state="readonly", font=(FONT, 10))
        self.cb_idle.grid(row=5, column=1, sticky="w", pady=3)

        self._label(inner, "生效时段留空=全天；跨午夜写 22:00-07:00", 8, MUTED).grid(
            row=6, column=0, columnspan=4, sticky="w", pady=(2, 6))

        btns = tk.Frame(inner, bg=CARD)
        btns.grid(row=7, column=0, columnspan=4, sticky="w")
        self._btn(btns, "保存设置", self._save_settings, primary=True).pack(side="left")
        self._btn(btns, "恢复默认曲线", self._reset_curve).pack(side="left", padx=8)

    def _build_curve(self, parent) -> None:
        card = self._card(parent)
        card.pack(fill="both", expand=True)
        head = tk.Frame(card, bg=CARD)
        head.pack(fill="x", padx=16, pady=(12, 6))
        self._label(head, "温度 → 档位 曲线", 11, FG, True).pack(side="left")
        self.btn_cpu = self._btn(head, "CPU", lambda: self._switch_curve("cpu"))
        self.btn_gpu = self._btn(head, "GPU", lambda: self._switch_curve("gpu"))
        self.btn_gpu.pack(side="right")
        self.btn_cpu.pack(side="right", padx=6)

        self.curve = CurveEditor(card, self.engine.cfg["sensors"][0]["curve"],
                                 self._curve_changed, height=230)
        self.curve.pack(fill="both", expand=True, padx=14)
        foot = tk.Frame(card, bg=CARD)
        foot.pack(fill="x", padx=16, pady=(6, 12))
        for _name in ("静音", "均衡", "散热"):
            self._btn(foot, _name,
                      lambda n=_name: self._apply_preset(n)).pack(side="left", padx=(0, 6))
        self.lbl_curve_hint = self._label(foot, "拖动圆点改档位；右键删点", 8, MUTED)
        self.lbl_curve_hint.pack(side="left", padx=(8, 0))
        self._btn(foot, "加点", self._add_point).pack(side="right")

    def _build_system(self, parent) -> None:
        card = self._card(parent)
        card.pack(fill="x", pady=(10, 0))
        inner = tk.Frame(card, bg=CARD)
        inner.pack(fill="x", padx=16, pady=12)
        self._label(inner, "系统", 11, FG, True).grid(row=0, column=0, columnspan=4,
                                                     sticky="w", pady=(0, 8))
        self._btn(inner, "开启 LHM 温度", self._lhm_guide).grid(row=1, column=0, sticky="w")
        self.var_auto = tk.BooleanVar(value=_autostart_enabled())
        self.chk_auto = tk.Checkbutton(
            inner, text="开机自启", variable=self.var_auto,
            command=self._toggle_autostart, bg=CARD, fg=FG, selectcolor=CARD2,
            activebackground=CARD, activeforeground=FG, font=(FONT, 10),
            highlightthickness=0, bd=0)
        self.chk_auto.grid(row=1, column=1, sticky="w", padx=(10, 0))
        self._btn(inner, "打开目录", self._open_dir).grid(row=1, column=2, sticky="w",
                                                        padx=(10, 0))
        self.var_silent = tk.BooleanVar(
            value=bool(self.engine.cfg["purifier"].get("silent", True)))
        self.chk_silent = tk.Checkbutton(
            inner, text="静音", variable=self.var_silent,
            command=self._toggle_silent, bg=CARD, fg=FG, selectcolor=CARD2,
            activebackground=CARD, activeforeground=FG, font=(FONT, 10),
            highlightthickness=0, bd=0)
        self.chk_silent.grid(row=1, column=3, sticky="w", padx=(10, 0))
        self.lbl_hint = self._label(inner, "LHM 需要以管理员运行并打开 Web Server",
                                    8, MUTED)
        self.lbl_hint.grid(row=2, column=0, columnspan=4, sticky="w", pady=(6, 0))

    def _build_log(self, parent) -> None:
        wrap = tk.Frame(parent, bg=BG, height=168)
        wrap.pack(fill="x", pady=(10, 0), side="bottom")
        wrap.pack_propagate(False)
        bar = tk.Frame(wrap, bg=BG)
        bar.pack(fill="x")
        self._btn(bar, "开始控制", self._start, primary=True).pack(side="left")
        self._btn(bar, "暂停", self._pause).pack(side="left", padx=6)
        self._btn(bar, "交还自动", self._release).pack(side="left")
        self._btn(bar, "试 8 档", lambda: self._try(8)).pack(side="left", padx=6)
        self._btn(bar, "试 16 档", lambda: self._try(16)).pack(side="left")

        self.log = tk.Text(wrap, height=4, bg="#11151b", fg="#a9b4c4", relief="flat",
                           font=("Consolas", 9), insertbackground=FG)
        self.log.pack(fill="both", expand=True, pady=(8, 0))
        self.log.configure(state="disabled")

    def _btn(self, parent, text: str, cmd, primary: bool = False) -> tk.Button:
        bg = ACCENT if primary else CARD2
        fg = "#ffffff" if primary else FG
        b = tk.Button(parent, text=text, command=cmd, bg=bg, fg=fg,
                      activebackground="#3f78e0" if primary else "#2f3746",
                      activeforeground=fg, relief="flat", bd=0, padx=12, pady=5,
                      font=(FONT, 9), cursor="hand2")
        return b

    # -- 曲线 -------------------------------------------------------------
    def _sensor_index(self, name: str) -> int:
        for i, s in enumerate(self.engine.cfg["sensors"]):
            if (s.get("name") or s["kind"]) == name:
                return i
        return 0

    def _switch_curve(self, name: str) -> None:
        self.curve_target = name
        self._refresh_curve()
        self.btn_cpu.configure(bg=ACCENT if name == "cpu" else CARD2)
        self.btn_gpu.configure(bg=ACCENT if name == "gpu" else CARD2)

    def _refresh_curve(self) -> None:
        idx = self._sensor_index(self.curve_target)
        self.curve.set_points(self.engine.cfg["sensors"][idx]["curve"])

    def _curve_changed(self, points: List[List[float]]) -> None:
        idx = self._sensor_index(self.curve_target)
        self.engine.cfg["sensors"][idx]["curve"] = points
        bridge.save_config(self.engine.cfg, CONFIG_PATH)
        self.engine.apply_config(self.engine.cfg)
        LOG.info("%s 曲线更新：%s", self.curve_target.upper(),
                 " ".join("%d→%d" % (t, l) for t, l in points))

    def _apply_preset(self, name: str) -> None:
        preset = PRESETS.get(name)
        if not preset:
            return
        for spec in self.engine.cfg["sensors"]:
            key = (spec.get("name") or spec["kind"]).lower()
            if key in preset:
                spec["curve"] = [list(pt) for pt in preset[key]]
        self.engine.apply_config(self.engine.cfg)
        self._refresh_curve()
        LOG.info("套用曲线预设：%s", name)
        self.lbl_hint.configure(text="已套用「%s」预设曲线" % name, fg=OK)

    def _add_point(self) -> None:
        pts = sorted(self.curve.points)
        if len(pts) >= 2:
            mid = (pts[0][0] + pts[-1][0]) / 2
            lv = bridge.curve_value(pts, mid)
            self.curve.points.append([round(mid), round(lv)])
            self.curve._normalize()
            self.curve.redraw()
            self._curve_changed(self.curve.export())

    def _reset_curve(self) -> None:
        idx = self._sensor_index(self.curve_target)
        default = DEFAULT_CONFIG["sensors"][1 if self.curve_target == "gpu" else 0]
        self.engine.cfg["sensors"][idx]["curve"] = [list(p) for p in default["curve"]]
        self._refresh_curve()
        self.engine.apply_config(self.engine.cfg)

    # -- 设置 -------------------------------------------------------------
    def _sync_settings(self) -> None:
        c = self.engine.cfg["control"]
        self.e_interval.delete(0, "end")
        self.e_interval.insert(0, str(c.get("interval_s", 10)))
        self.e_gap.delete(0, "end")
        self.e_gap.insert(0, str(c.get("min_command_gap_s", 30)))
        self.e_min.delete(0, "end")
        self.e_min.insert(0, str(c.get("min_level", 0)))
        self.e_max.delete(0, "end")
        self.e_max.insert(0, str(c.get("max_level", 16)))
        self.e_dead.delete(0, "end")
        self.e_dead.insert(0, str(c.get("deadband", 1)))
        self.e_idle.delete(0, "end")
        self.e_idle.insert(0, str(c.get("idle_release_min", 10)))
        self.e_hours.delete(0, "end")
        self.e_hours.insert(0, ",".join(c.get("active_hours") or []))
        self.cb_idle.set(str(c.get("idle_action", "auto")))

    def _save_settings(self) -> None:
        c = self.engine.cfg["control"]
        try:
            c["interval_s"] = float(self.e_interval.get())
            c["min_command_gap_s"] = float(self.e_gap.get())
            c["min_level"] = int(self.e_min.get())
            c["max_level"] = int(self.e_max.get())
            c["deadband"] = int(self.e_dead.get())
            c["idle_release_min"] = float(self.e_idle.get())
        except ValueError:
            messagebox.showerror("参数不对", "请填数字")
            return
        hours = self.e_hours.get().strip()
        c["active_hours"] = [h.strip() for h in hours.split(",") if h.strip()]
        c["idle_action"] = self.cb_idle.get() or "auto"
        self.engine.apply_config(self.engine.cfg)
        LOG.info("设置已保存")
        self.lbl_hint.configure(text="设置已保存", fg=OK)

    # -- 控制动作 ---------------------------------------------------------
    def _start(self) -> None:
        try:
            self.engine.start_control()
        except Exception as ex:
            messagebox.showerror("无法开始", str(ex))
            return
        self.lbl_hint.configure(text="控制已开始", fg=OK)

    def _pause(self) -> None:
        self.engine.stop_control()
        self.lbl_hint.configure(text="已暂停（设备保持当前档位）", fg=WARN)

    def _release(self) -> None:
        try:
            self.engine.release()
            self.lbl_hint.configure(text="已交还自动模式", fg=OK)
        except Exception as ex:
            messagebox.showerror("交还失败", str(ex))

    def _try(self, level: int) -> None:
        try:
            self.engine.try_level(level)
            self.engine.refresh_device()
        except Exception as ex:
            messagebox.showerror("试档失败", str(ex))

    # -- LHM 温度：引导用户打开 Web Server ---------------------------------
    def _lhm_guide(self) -> None:
        if _port_open(8085):
            messagebox.showinfo("LHM 已就绪", "CPU 温度正在读取，不需要再设置。")
            return
        if _lhm_running():
            self._focus_lhm()
            self._show_lhm_tip()
            return
        self._start_lhm()
        self._lhm_wait = True  # 等它起来后自动把窗口拉到前面 + 弹提示

    def _start_lhm(self) -> None:
        if not os.path.exists(LHM_EXE):
            messagebox.showerror("找不到 LHM", LHM_EXE)
            return
        try:
            subprocess.Popen(
                ["powershell", "-NoProfile", "-Command",
                 "Start-Process -FilePath '%s' -WorkingDirectory '%s' -Verb RunAs"
                 % (LHM_EXE, os.path.dirname(LHM_EXE))],
                creationflags=0x08000000)
            self.lbl_hint.configure(text="已请求启动 LHM，请在 UAC 窗口点“是”", fg=WARN)
        except Exception as ex:
            messagebox.showerror("启动失败", str(ex))

    def _focus_lhm(self) -> None:
        try:
            user32 = ctypes.windll.user32
            for pid in _lhm_pids():
                hwnd = _main_window_of(pid)
                if hwnd:
                    user32.ShowWindow(hwnd, 5)   # SW_SHOW：把托盘里隐藏的窗口显示出来
                    user32.ShowWindow(hwnd, 9)   # SW_RESTORE
                    user32.SetForegroundWindow(hwnd)
                    LOG.info("已把 LHM 窗口调到前台（pid=%s）", pid)
                    return
            LOG.warning("没找到 LHM 的主窗口（可能还最小化在托盘里）")
        except Exception as ex:
            LOG.debug("聚焦 LHM 失败：%s", ex)

    def _show_lhm_tip(self) -> None:
        tip = getattr(self, "_lhm_tip", None)
        if tip is not None and tip.winfo_exists():
            tip.lift()
            return
        tip = tk.Toplevel(self)
        self._lhm_tip = tip
        tip.title("开一下 LHM 的 Web Server")
        tip.configure(bg=CARD)
        tip.attributes("-topmost", True)
        tip.resizable(False, False)
        tip.geometry("+%d+%d" % (self.winfo_rootx() + self.winfo_width() - 430,
                                 self.winfo_rooty() + 90))
        tk.Label(tip, text="两步就好", bg=CARD, fg=FG,
                 font=(FONT, 12, "bold")).pack(anchor="w", padx=18, pady=(16, 6))
        tk.Label(tip, bg=CARD, fg=FG, justify="left", font=(FONT, 10),
                 text=("在 LibreHardwareMonitor 窗口里点：\n\n"
                       "    Options → Remote Web Server → Run\n\n"
                       "勾上后本窗口会自动关闭，温度立刻开始读。")).pack(
            anchor="w", padx=18)
        tk.Label(tip, text="每次重启电脑后都要点一次（LHM 不保存这个开关）",
                 bg=CARD, fg=MUTED, font=(FONT, 8)).pack(anchor="w", padx=18, pady=(8, 0))
        tk.Button(tip, text="关掉提示", command=tip.destroy, bg=CARD2, fg=FG,
                  relief="flat", bd=0, padx=12, pady=4, font=(FONT, 9),
                  activebackground="#2f3746", activeforeground=FG).pack(
            anchor="e", padx=18, pady=12)

        def poll() -> None:
            if not tip.winfo_exists():
                return
            if _port_open(8085):
                tip.destroy()
                LOG.info("LHM Web Server 已开启，恢复读取 CPU 温度")
                self.lbl_hint.configure(text="LHM 温度已就绪", fg=OK)
                self.engine._fail_until.clear()
                return
            tip.after(1000, poll)

        tip.after(600, poll)

    def _toggle_autostart(self) -> None:
        want = bool(self.var_auto.get())
        LOG.info("点击开机自启 -> %s", "开" if want else "关")
        try:
            _set_autostart(want)
        except Exception as ex:
            LOG.exception("设置开机自启失败")
            self.var_auto.set(not want)
            self.lbl_hint.configure(text="设置失败：被安全软件拦截", fg=BAD)
            try:
                subprocess.Popen(["explorer", "shell:startup"], creationflags=0x08000000)
            except Exception:
                pass
            messagebox.showerror("设置开机自启失败", str(ex))
            return
        ok = _autostart_enabled()
        self.var_auto.set(ok)
        if ok:
            self.lbl_hint.configure(
                text="开机自启已开启：登录后静默运行（沿用上次运行状态）", fg=OK)
        else:
            self.lbl_hint.configure(text="开机自启已关闭", fg=MUTED)
        LOG.info("开机自启状态 -> %s", ok)

    def _toggle_silent(self) -> None:
        on = bool(self.var_silent.get())
        self.engine.cfg["purifier"]["silent"] = on
        self.engine.apply_config(self.engine.cfg)
        LOG.info("切档静音 -> %s", "开" if on else "关")
        self.lbl_hint.configure(
            text="切档静音已开启（不再“滴”，交还时恢复）" if on else "切档静音已关闭",
            fg=OK if on else MUTED)

    def _open_dir(self) -> None:
        subprocess.Popen(["explorer", BASE_DIR])

    # -- 托盘 -------------------------------------------------------------
    def _init_tray(self) -> None:
        try:
            icon = os.path.join(BASE_DIR, "icon.ico")
            tray_mod.make_icon(icon)
            self.tray = tray_mod.Tray(icon, "散热管家 · 后台控制中",
                                      on_action=self._tray_q.put, log=LOG)
            self.tray.start()
            self.tray.wait_ready(3.0)
        except Exception as ex:
            LOG.warning("托盘初始化失败：%s", ex)
            self.tray = None

    def _tray_action(self, cmd: int) -> None:
        if cmd == tray_mod.ID_SHOW:
            self._show_window()
        elif cmd == tray_mod.ID_PAUSE:
            if self.engine.control_on:
                self._pause()
                self.tray and self.tray.notify("已暂停", "净化器保持当前档位。")
            else:
                self._start()
                self.tray and self.tray.notify("已继续", "从上次温度继续控制。")
        elif cmd in (tray_mod.ID_PRESET_QUIET, tray_mod.ID_PRESET_BALANCE,
                     tray_mod.ID_PRESET_COOL):
            name = {tray_mod.ID_PRESET_QUIET: "静音",
                    tray_mod.ID_PRESET_BALANCE: "均衡",
                    tray_mod.ID_PRESET_COOL: "散热"}[cmd]
            self._apply_preset(name)
            self.tray and self.tray.notify("已切到「%s」" % name, "曲线已更新并保存。")
        elif cmd == tray_mod.ID_RELEASE_EXIT:
            self._exit_app(confirm=True)

    def _exit_app(self, confirm: bool = True) -> None:
        if confirm:
            self.deiconify()
            self.lift()
            if not messagebox.askokcancel("退出", "退出后控制会停止，并把净化器交还自动模式。继续？"):
                return
        self._closing = True
        try:
            self.engine.shutdown()
        finally:
            if self.tray is not None:
                try:
                    self.tray.remove()
                except Exception:
                    pass
            self.destroy()
            os._exit(0)

    # -- 定时刷新 ---------------------------------------------------------
    def _tick(self) -> None:
        if self._closing:
            return
        try:
            while not self._tray_q.empty():
                self._tray_action(self._tray_q.get_nowait())
            self._update_live()
            self._update_pills()
            self._update_log()
            self._check_offline()
            self._tick_n += 1
            if self._tick_n % 5 == 0:
                if self.tray is not None:
                    self.tray.set_tooltip(self._tooltip_text())
                self.title("散热管家 · " + self._tooltip_text().replace("散热管家 · ", ""))
        except Exception:
            # 界面循环里的异常以前是静默吞掉的，正是"点了没反应"查不出原因的原因
            self._tick_errors = getattr(self, "_tick_errors", 0) + 1
            if self._tick_errors <= 3 or self._tick_errors % 200 == 0:
                LOG.exception("界面刷新异常（第 %d 次）", self._tick_errors)
        self.after(1000, self._tick)

    def _tooltip_text(self) -> str:
        cpu = (self.engine.temps or {}).get("cpu") or {}
        gpu = (self.engine.temps or {}).get("gpu") or {}
        dev = self.engine.device or {}
        parts = []
        if "value" in cpu:
            unit = "%" if "%" in str(cpu.get("source", "")) else "℃"
            parts.append("CPU %.0f%s" % (cpu["value"], unit))
        if "value" in gpu:
            parts.append("GPU %.0f℃" % gpu["value"])
        if dev.get("connected"):
            mode = (dev.get("mode") or "").lower()
            if mode == "favorite":
                parts.append("%s档" % dev.get("favorite_level"))
            else:
                parts.append("%s模式" % {"auto": "自动", "silent": "睡眠", "idle": "待机"}.get(mode, mode or "?"))
        parts.append("运行中" if self.engine.control_on else "已暂停")
        return "散热管家 · " + " · ".join(p for p in parts if p)

    def _check_offline(self) -> None:
        """净化器掉线超过 5 分钟提醒一次，恢复时再提醒一次。"""
        connected = bool(self.engine.device.get("connected"))
        if connected:
            if self._off_notified:
                self._off_notified = False
                self._off_since = 0.0
                LOG.info("净化器恢复连接")
                if self.tray is not None:
                    self.tray.notify("净化器已重新连接", "继续按温度控制。")
            return
        if self._off_since == 0.0:
            self._off_since = time.time()
        elif not self._off_notified and time.time() - self._off_since > 300:
            self._off_notified = True
            LOG.warning("净化器掉线超过 5 分钟")
            if self.tray is not None:
                self.tray.notify("净化器掉线了",
                                 "已断开 5 分钟以上，检查电源和 Wi-Fi。", warn=True)

    def _update_live(self) -> None:
        with self.engine.lock:
            temps = dict(self.engine.temps)
            dev = dict(self.engine.device)
            running = self.engine.control_on
            last = dict(self.engine.ctrl.last) if (self.engine.ctrl and self.engine.ctrl.last) else {}

        for name, lb in (("cpu", self.lbl_cpu), ("gpu", self.lbl_gpu)):
            t = temps.get(name) or {}
            if "value" in t:
                is_load = "%" in str(t.get("source", ""))
                lb.configure(text=("%.0f%%" if is_load else "%.0f℃") % t["value"],
                             fg=MUTED if t.get("fallback") else FG)
            elif "error" in t:
                lb.configure(text="—", fg=MUTED)
            else:
                lb.configure(text="…", fg=MUTED)

        cpu = (temps.get("cpu") or {}).get("value")
        self.curve.set_marker(cpu if cpu is not None else None)

        if dev.get("connected"):
            lvl = dev.get("favorite_level")
            rpm = dev.get("motor1_speed")
            mode = (dev.get("mode") or "").lower()
            if mode == "favorite":
                text = "当前 %s 档 · %s rpm · 最爱模式" % (lvl, rpm)
            else:
                # auto / silent / idle 模式下 favorite_level 只是记忆值，不是实际风速
                text = "%s模式 · %s rpm（记忆 %s 档）" % (
                    {"auto": "自动", "silent": "睡眠", "idle": "待机", "manual": "手动"}.get(mode, mode or "未知"),
                    rpm, lvl)
            self.lbl_level.configure(text=text, fg=FG if running else MUTED)
            self.lbl_extra.configure(
                text="AQI %s · 机内温度 %.1f℃" % (dev.get("aqi"),
                                                (dev.get("temp_dec") or 0) / 10))
        else:
            self.lbl_level.configure(text="净化器未连接", fg=BAD)
            self.lbl_extra.configure(text="")

        target = last.get("target")
        self.lbl_target.configure(
            text="目标 %s 档 · %s" % (target, "运行中" if running else "已暂停")
            if target is not None else ("运行中" if running else "已暂停"))

    def _update_pills(self) -> None:
        if getattr(self, "_lhm_wait", False) and _lhm_running() and not _port_open(8085):
            self._lhm_wait = False
            self._focus_lhm()
            self._show_lhm_tip()
        dev = self.engine.device
        if dev.get("connected"):
            self.pill_dev.configure(text="● 净化器 已连接", fg=OK)
        else:
            self.pill_dev.configure(text="● 净化器 未连接", fg=BAD)

        lhm_ok = _port_open(8085)
        if lhm_ok and not getattr(self, "_lhm_was_ok", True):
            self.engine._fail_until.clear()   # 端口刚开，立刻重试采样
            LOG.info("LHM Web Server 已就绪，恢复读取 CPU 温度")
        self._lhm_was_ok = lhm_ok
        if getattr(self, "_lhm_wait", False) and lhm_ok:
            self._lhm_wait = False

        cpu_info = (self.engine.temps or {}).get("cpu") or {}
        if cpu_info.get("fallback"):
            # 没有 LHM 时用 CPU 负载顶替，属于正常工作状态，不报警
            self.pill_lhm.configure(text="● CPU 负载估算", fg=MUTED)
        elif lhm_ok:
            self.pill_lhm.configure(text="● LHM 就绪", fg=OK)
        else:
            self.pill_lhm.configure(text="● LHM 未就绪", fg=WARN)

        if self.engine.control_on:
            self.pill_run.configure(text="● 控制中", fg=ACCENT)
        else:
            self.pill_run.configure(text="● 已暂停", fg=MUTED)

    def _update_log(self) -> None:
        lines = RING.snapshot(120)
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.insert("end", "\n".join(lines))
        self.log.see("end")
        self.log.configure(state="disabled")

    # -- 单实例 / IPC -----------------------------------------------------
    def _start_ipc(self) -> None:
        self.ipc = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            self.ipc.bind(("127.0.0.1", IPC_PORT))
            self.ipc.listen(2)
            threading.Thread(target=self._ipc_loop, daemon=True).start()
        except OSError:
            self.ipc = None  # 已有实例在跑

    def _ipc_loop(self) -> None:
        while not self._closing:
            try:
                conn, _ = self.ipc.accept()
            except OSError:
                return
            try:
                conn.settimeout(1.0)
                data = conn.recv(16)
                if data.strip() == b"SHOW":
                    self.after(0, self._show_window)
            except Exception:
                pass
            finally:
                conn.close()

    def _show_window(self) -> None:
        self.deiconify()
        self.lift()
        self.attributes("-topmost", True)
        self.after(600, lambda: self.attributes("-topmost", False))

    def _on_close(self) -> None:
        # 关窗口 = 缩到托盘，控制继续（真正退出走托盘菜单）
        self.withdraw()
        LOG.info("窗口已收起，继续在后台控制")
        if self.tray is not None:
            self.tray.notify("散热管家还在后台跑",
                             "双击托盘图标可重新打开；右键可退出。")


# --------------------------------------------------------------------------
# 开机自启
# --------------------------------------------------------------------------
STARTUP_DIR = os.path.join(os.environ.get("APPDATA", ""),
                           r"Microsoft\Windows\Start Menu\Programs\Startup")
LNK_NAME = "散热管家.lnk"
VBS_NAME = "散热管家.vbs"


def _startup_paths() -> "tuple[str, str]":
    return (os.path.join(STARTUP_DIR, LNK_NAME), os.path.join(STARTUP_DIR, VBS_NAME))


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "散热管家"


def _run_key_enabled() -> bool:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, RUN_NAME)
        return True
    except OSError:
        return False


def _run_key_set(on: bool, target: str, args: str) -> bool:
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_SET_VALUE) as key:
            if on:
                winreg.SetValueEx(key, RUN_NAME, 0, winreg.REG_SZ,
                                  '"%s" %s' % (target, args))
            else:
                try:
                    winreg.DeleteValue(key, RUN_NAME)
                except FileNotFoundError:
                    pass
        return True
    except OSError as ex:
        LOG.warning("写注册表 Run 失败：%s", ex)
        return False


def _autostart_enabled() -> bool:
    """开机自启：注册表 Run 或「启动」文件夹里任一存在即视为开启。"""
    lnk, vbs = _startup_paths()
    return _run_key_enabled() or os.path.exists(lnk) or os.path.exists(vbs)


def _autostart_target() -> "tuple[str, str, str]":
    """返回 (可执行文件, 参数, 工作目录)。"""
    if getattr(sys, "frozen", False):
        return sys.executable, "--silent", BASE_DIR
    target = sys.executable
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if os.path.exists(pyw):
        target = pyw
    return target, '"%s" --silent' % os.path.abspath(__file__), BASE_DIR


def _make_lnk(dest: str, target: str, args: str, workdir: str) -> bool:
    """用 PowerShell 的 WScript.Shell 建 .lnk，成功返回 True。"""
    script = (
        "$ErrorActionPreference='Stop'; "
        "$ws = New-Object -ComObject WScript.Shell; "
        "$s = $ws.CreateShortcut('%s'); "
        "$s.TargetPath = '%s'; $s.Arguments = '%s'; $s.WorkingDirectory = '%s'; "
        "$s.WindowStyle = 7; $s.Description = '散热管家'; $s.Save()"
    ) % (dest, target, args, workdir)
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                            "-Command", script],
                           capture_output=True, stdin=subprocess.DEVNULL, timeout=30,
                           creationflags=0x08000000)
        if os.path.exists(dest):
            return True
        err = (r.stderr or r.stdout).decode("gbk", "replace").strip()
        LOG.warning("建快捷方式失败 rc=%s %s", r.returncode, err[:200])
    except Exception as ex:
        LOG.warning("建快捷方式异常：%s", ex)
    return False


def _set_autostart(on: bool) -> None:
    lnk, vbs = _startup_paths()
    target, args, workdir = _autostart_target()

    if not on:
        _run_key_set(False, target, args)
        for path in (lnk, vbs):
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
            except OSError as ex:
                raise RuntimeError("删除启动项失败：%s" % ex) from ex
        if _autostart_enabled():
            raise RuntimeError("删除失败，仍检测到启动项（可能被安全软件保护）")
        LOG.info("开机自启：已关闭")
        return

    LOG.info("开机自启：target=%s args=%s workdir=%s", target, args, workdir)

    # 方案一：注册表 Run（最省事，不依赖其它组件）
    if _run_key_set(True, target, args) and _run_key_enabled():
        LOG.info("开机自启：已写入注册表 Run")
        return

    # 方案二：启动文件夹里的 .lnk
    if _make_lnk(lnk, target, args, workdir):
        LOG.info("开机自启：已创建快捷方式 %s", lnk)
        return

    # 方案三：启动文件夹里的 .vbs
    body = (
        'Set sh = CreateObject("WScript.Shell")\r\n'
        'sh.CurrentDirectory = "%s"\r\n'
        'sh.Run """%s"" %s", 0, False\r\n'
    ) % (workdir, target, args)
    try:
        with open(vbs, "w", encoding="mbcs") as fh:
            fh.write(body)
        if os.path.exists(vbs):
            LOG.info("开机自启：已写入启动脚本 %s", vbs)
            return
    except OSError as ex:
        LOG.warning("写入启动脚本失败：%s", ex)

    # 全被拦：造一个现成的快捷方式，让用户自己拖进启动文件夹
    drag = os.path.join(BASE_DIR, "开机自启-拖进启动文件夹.lnk")
    made = _make_lnk(drag, target, args, workdir)
    LOG.error("开机自启：三种方式都被拒绝（安全软件拦截），拖拽捷径=%s made=%s", drag, made)
    if made:
        raise RuntimeError(
            "被安全软件拦住了（火绒/360 常见）。\n\n"
            "已经给你生成一个现成的快捷方式：\n%s\n\n"
            "现在会打开「启动」文件夹，把这个文件拖进去就完成了（只需一次）。"
            % drag)
    raise RuntimeError(
        "被安全软件拦住了（火绒/360 常见），也没能生成快捷方式。\n\n"
        "办法：在火绒里把「散热管家」加入信任，或手动在「启动」文件夹里放一个指向\n%s 的快捷方式。"
        % sys.executable)


def _lhm_pids() -> "list[int]":
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq LibreHardwareMonitor.exe",
             "/FO", "CSV", "/NH"],
            capture_output=True, stdin=subprocess.DEVNULL, timeout=10,
            creationflags=0x08000000).stdout.decode("gbk", "replace")
        pids = []
        for line in out.strip().splitlines():
            parts = [p.strip().strip('"') for p in line.split('","')]
            if len(parts) >= 2 and parts[0].lower().startswith("librehardwaremonitor"):
                try:
                    pids.append(int(parts[1]))
                except ValueError:
                    pass
        return pids
    except Exception:
        return []


def _main_window_of(pid: int) -> int:
    """找 LHM 的主窗口；托盘里"隐藏"的窗口也要能找出来（之后 ShowWindow 还原）。"""
    found: "list[int]" = []
    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    user32 = ctypes.windll.user32

    def _cb(hwnd, _lparam):
        wnd_pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wnd_pid))
        if wnd_pid.value != pid:
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        title = buf.value or ""
        if "Libre Hardware Monitor" in title and "Broadcast" not in title:
            found.append(int(hwnd))
        return True

    try:
        user32.EnumWindows(WNDENUMPROC(_cb), 0)
    except Exception:
        return 0
    return found[0] if found else 0


def _lhm_running() -> bool:
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq LibreHardwareMonitor.exe"],
                             capture_output=True, stdin=subprocess.DEVNULL, timeout=10,
                             creationflags=0x08000000).stdout.decode("gbk", "replace")
        return "LibreHardwareMonitor.exe" in out
    except Exception:
        return False


def _port_open(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.25)
        return s.connect_ex((host, port)) == 0


# --------------------------------------------------------------------------
def setup_logging() -> None:
    os.makedirs(LOG_DIR, exist_ok=True)
    LOG.setLevel(logging.INFO)
    if not LOG.handlers:
        fmt = logging.Formatter("%(asctime)s %(levelname)-5s %(message)s", "%H:%M:%S")
        fh = RotatingFileHandler(os.path.join(LOG_DIR, "app.log"),
                                 maxBytes=2_000_000, backupCount=3,
                                 encoding="utf-8")
        fh.setFormatter(fmt)
        LOG.addHandler(fh)
        RING.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
        LOG.addHandler(RING)


_MUTEX_HANDLE = None


def _single_instance() -> bool:
    """命名互斥体做单实例；已经有实例就返回 False。"""
    global _MUTEX_HANDLE
    try:
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        handle = kernel32.CreateMutexW(None, False, "Local\\PurifierBridgeApp")
        if not handle:
            return True
        _MUTEX_HANDLE = handle
        return kernel32.GetLastError() != 183  # ERROR_ALREADY_EXISTS
    except Exception:
        return True


def main() -> None:
    silent = "--silent" in sys.argv
    if "--set-autostart" in sys.argv:
        setup_logging()
        i = sys.argv.index("--set-autostart")
        val = sys.argv[i + 1].lower() if len(sys.argv) > i + 1 else "on"
        on = val in ("on", "1", "true", "yes")
        try:
            _set_autostart(on)
            LOG.info("设置开机自启=%s -> 结果=%s", on, _autostart_enabled())
        except Exception:
            LOG.exception("设置开机自启失败")
        return

    if "--check-autostart" in sys.argv:
        setup_logging()
        LOG.info("== 自检：开机自启 ==")
        LOG.info("STARTUP_DIR=%s", STARTUP_DIR)
        LOG.info("before=%s", _autostart_enabled())
        try:
            _set_autostart(True)
            LOG.info("enable -> %s", _autostart_enabled())
            _set_autostart(False)
            LOG.info("disable -> %s", _autostart_enabled())
            LOG.info("结果：%s", "正常" if not _autostart_enabled() else "异常（关不掉）")
        except Exception:
            LOG.exception("自检失败")
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    setup_logging()

    # 已有实例在跑 → 让它亮出窗口
    if not _single_instance() or _port_open(IPC_PORT):
        try:
            with socket.create_connection(("127.0.0.1", IPC_PORT), timeout=1) as s:
                s.sendall(b"SHOW")
        except Exception:
            pass
        return

    trace('main: creating App')
    app = App(silent=silent)
    trace('main: App created')
    if not silent:
        app._show_window()
    LOG.info("进入事件循环")
    app.mainloop()


if __name__ == "__main__":
    main()
