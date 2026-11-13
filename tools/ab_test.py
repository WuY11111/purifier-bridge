"""A/B 对照：净化器风量对 CPU 稳态温度的影响。

同样的 6 进程满载，比较「净化器 0 档」和「16 档」时的 CPU Package 稳态温度，
用来判断：外置风道到底值多少度，联想那边的风扇曲线该不该放松。

    python tools/ab_test.py
"""

import subprocess
import sys
import time

sys.path.insert(0, __file__.rsplit("\\", 2)[0])

import miio_lan  # noqa: E402
import sensors  # noqa: E402

IP = "192.168.31.203"
TOKEN = "bb7d117c7640473ea89f651e1e9004e1"
BURN = __file__.replace("ab_test.py", "burn.py")
LOAD_PROCS = 6
WARMUP = 25
MEASURE = 60


def cpu_temp():
    return sensors.pick(sensors.lhm_web(), ["CPU Package"]).value


def set_level(dev, level):
    dev.set_power(True)
    dev.set_mode("favorite")
    dev.set_favorite_level(level)
    print("  净化器 -> %d 档" % level, flush=True)


def phase(dev, label, level, burn_secs):
    print("== %s：净化器 %d 档 ==" % (label, level), flush=True)
    set_level(dev, level)
    time.sleep(3)

    procs = [subprocess.Popen([sys.executable, BURN, str(burn_secs)],
                              creationflags=0x08000000, stdout=subprocess.DEVNULL)
             for _ in range(LOAD_PROCS)]
    t0 = time.time()
    samples = []
    while time.time() - t0 < WARMUP + MEASURE:
        el = time.time() - t0
        try:
            v = cpu_temp()
        except Exception as ex:
            print("  读取失败", ex, flush=True)
            v = None
        if v is not None:
            samples.append((el, v))
            stage = "预热" if el < WARMUP else "测量"
            print("  %s t=%3.0fs  CPU %.1f" % (stage, el, v), flush=True)
        time.sleep(5)
    for p in procs:
        p.terminate()
    steady = [v for t, v in samples if t >= WARMUP]
    ss = sum(steady) / len(steady) if steady else float("nan")
    print("  => 稳态均值 %.1f ℃（后 %d 个采样）" % (ss, len(steady)), flush=True)
    return ss


def main():
    dev = miio_lan.MiioDevice(IP, TOKEN, timeout=4)
    print("初始:", dev.status(), flush=True)

    a = phase(dev, "A 对照组", 0, WARMUP + MEASURE + 10)
    print("冷却 45 秒…", flush=True)
    time.sleep(45)
    b = phase(dev, "B 外置风量全开", 16, WARMUP + MEASURE + 10)

    print("\n结果：0 档 %.1f℃ / 16 档 %.1f℃ → 外置风道降低 %.1f℃"
          % (a, b, a - b), flush=True)

    dev.set_power(True)
    dev.set_mode("auto")
    print("已交还 auto:", dev.status(), flush=True)


if __name__ == "__main__":
    main()
