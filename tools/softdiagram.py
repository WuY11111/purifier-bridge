"""用 Pillow 画说明书里的两张示意图（系统架构、控制流程）。"""

from __future__ import annotations

import os

from PIL import Image, ImageDraw, ImageFont

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FONT = "C:/Windows/Fonts/msyh.ttc"
BG = (250, 251, 253)
BOX = (232, 240, 252)
EDGE = (79, 140, 255)
TEXT = (28, 34, 45)
GREY = (110, 120, 135)


def font(size):
    return ImageFont.truetype(FONT, size)


def box(d, xy, text, sub=None, fg=BOX, radius=10):
    d.rounded_rectangle(xy, radius=radius, fill=fg, outline=EDGE, width=2)
    x0, y0, x1, y1 = xy
    f = font(19)
    lines = text.split("\n")
    total = len(lines) * 26 + (22 if sub else 0)
    y = y0 + ((y1 - y0) - total) // 2
    for ln in lines:
        w = d.textlength(ln, font=f)
        d.text(((x0 + x1 - w) / 2, y), ln, font=f, fill=TEXT)
        y += 26
    if sub:
        f2 = font(14)
        sub_lines = sub.split("\n")
        for sl in sub_lines:
            w = d.textlength(sl, font=f2)
            d.text(((x0 + x1 - w) / 2, y + 2), sl, font=f2, fill=GREY)
            y += 20


def arrow(d, p1, p2, label=None, color=EDGE):
    d.line([p1, p2], fill=color, width=3)
    import math
    ang = math.atan2(p2[1] - p1[1], p2[0] - p1[0])
    L, W = 13, 7
    x, y = p2
    d.polygon([(x, y),
               (x - L * math.cos(ang) + W * math.sin(ang), y - L * math.sin(ang) - W * math.cos(ang)),
               (x - L * math.cos(ang) - W * math.sin(ang), y - L * math.sin(ang) + W * math.cos(ang))],
              fill=color)
    if label:
        f = font(14)
        mx, my = (p1[0] + p2[0]) / 2, (p1[1] + p2[1]) / 2
        w = d.textlength(label, font=f)
        d.rectangle([mx - w / 2 - 4, my - 11, mx + w / 2 + 4, my + 11], fill=BG)
        d.text((mx - w / 2, my - 9), label, font=f, fill=GREY)


def arch(path):
    W, H = 1400, 820
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    box(d, (40, 60, 300, 170), "温度来源", "LHM / nvidia-smi / 系统时间")
    box(d, (40, 250, 300, 360), "备用来源", "CPU 占用率估算")
    box(d, (380, 140, 700, 280), "采样模块", "sampler.py\n周期采样 · 降级策略")
    box(d, (780, 140, 1100, 280), "控制核心", "bridge.py\n平滑 · 插值 · 回差 · 限流")
    box(d, (1180, 140, 1360, 280), "协议层", "miio_lan.py\nAES-128-CBC")
    box(d, (1180, 380, 1360, 500), "空气净化器", "UDP 54321")
    box(d, (380, 380, 700, 500), "配置中心", "config.py\n默认值 · 校验 · 原子写")
    box(d, (780, 380, 1100, 500), "界面与托盘", "app.py / tray.py\n实时曲线 · 托盘菜单")
    box(d, (40, 620, 1360, 700), "本地磁盘：config.json · state.json · logs/（滚动日志）", None, fg=(240, 245, 235))

    arrow(d, (300, 115), (380, 175), "读取")
    arrow(d, (170, 250), (380, 240), "降级")
    arrow(d, (700, 210), (780, 210), "采样值")
    arrow(d, (1100, 210), (1180, 210), "档位")
    arrow(d, (1270, 280), (1270, 380), "指令/回读")
    arrow(d, (700, 440), (780, 440), "配置")
    arrow(d, (940, 280), (940, 380), "状态")
    arrow(d, (540, 500), (540, 620), "读写")
    arrow(d, (1270, 500), (1270, 620), "状态")

    d.text((40, 20), "图 2  系统结构与模块关系", font=font(22), fill=TEXT)
    img.save(path)
    return path


def flow(path):
    W, H = 1400, 760
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    box(d, (60, 90, 420, 170), "① 周期采样（默认 10s）")
    box(d, (60, 230, 420, 310), "② 指数平滑（0.35）")
    box(d, (60, 370, 420, 450), "③ 曲线插值 → 目标档位")
    box(d, (60, 510, 420, 590), "④ 与设备当前档位比较")
    box(d, (520, 510, 880, 590), "差值 < 回差（2）？", "是 → 不下发，本轮结束")
    box(d, (520, 370, 880, 450), "⑤ 距上次下发 ≥ 30s？", "否 → 顺延到下一轮")
    box(d, (520, 230, 880, 310), "⑥ 关闭提示音（可选）")
    box(d, (520, 90, 880, 170), "⑦ 发送 set_mode / set_level 指令")
    box(d, (980, 90, 1340, 170), "⑧ 回读状态、刷新界面")
    box(d, (960, 510, 1380, 600), "空闲计时", "低于下限持续 10 分钟 → 交还自动模式")

    arrow(d, (240, 170), (240, 230))
    arrow(d, (240, 310), (240, 370))
    arrow(d, (240, 450), (240, 510))
    arrow(d, (420, 550), (520, 550))
    arrow(d, (700, 510), (700, 450))
    arrow(d, (700, 370), (700, 310))
    arrow(d, (700, 230), (700, 170))
    arrow(d, (880, 130), (980, 130))
    arrow(d, (1170, 170), (1170, 510), "计时")

    d.text((60, 20), "图 3  单轮控制流程", font=font(22), fill=TEXT)
    img.save(path)
    return path


if __name__ == "__main__":
    a = arch(os.path.join(BASE, "docs", "doc-arch.png"))
    b = flow(os.path.join(BASE, "docs", "doc-flow.png"))
    print(a)
    print(b)
