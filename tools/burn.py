"""简单的 CPU 满载工具，用来验证「温度→档位」曲线。

    python burn.py 30      # 单进程满载 30 秒（要吃满多核就多开几个）

只为测试用，不影响主程序。
"""

import sys
import time

dur = float(sys.argv[1]) if len(sys.argv) > 1 else 30.0
end = time.time() + dur
x = 1.0001
while time.time() < end:
    for _ in range(50000):
        x = x * 1.000001 + 1e-6
        if x > 1e6:
            x = 1.0001
print("burn done %.1fs" % dur)
