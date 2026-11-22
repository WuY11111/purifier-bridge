"""生成软著申请用的「软件说明书」docx（再用 LibreOffice 转 PDF）。"""

from __future__ import annotations

import os

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Mm, Pt

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHOT = os.path.join(BASE, "docs", "screenshot.jpg")
ARCH = os.path.join(BASE, "docs", "doc-arch.png")
FLOW = os.path.join(BASE, "docs", "doc-flow.png")

NAME = "笔记本温度联动空气净化器风速控制系统"
VERSION = "V1.0"


def cn_font(run, size=11, bold=False, mono=False):
    name = "Consolas" if mono else "宋体"
    run.font.name = name
    run.font.size = Pt(size)
    run.font.bold = bold
    rpr = run._element.get_or_add_rPr()
    rf = rpr.find(qn("w:rFonts"))
    if rf is None:
        rf = OxmlElement("w:rFonts")
        rpr.append(rf)
    rf.set(qn("w:ascii"), name)
    rf.set(qn("w:hAnsi"), name)
    rf.set(qn("w:eastAsia"), "宋体")


def h1(doc, text):
    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(16)
    p.paragraph_format.space_after = Pt(8)
    cn_font(p.add_run(text), 16, True)


def h2(doc, text):
    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(10)
    p.paragraph_format.space_after = Pt(4)
    cn_font(p.add_run(text), 12.5, True)


def para(doc, text, indent=True, size=10.5):
    p = doc.add_paragraph()
    p.paragraph_format.first_line_indent = Mm(7) if indent else Mm(0)
    p.paragraph_format.space_after = Pt(5)
    p.paragraph_format.line_spacing = 1.45
    cn_font(p.add_run(text), size)


def bullet(doc, text):
    p = doc.add_paragraph()
    p.paragraph_format.left_indent = Mm(7)
    p.paragraph_format.space_after = Pt(3)
    p.paragraph_format.line_spacing = 1.35
    cn_font(p.add_run("· " + text), 10.5)


def code(doc, lines):
    for ln in lines:
        p = doc.add_paragraph()
        p.paragraph_format.space_after = Pt(0)
        p.paragraph_format.line_spacing = 1.15
        p.paragraph_format.left_indent = Mm(6)
        cn_font(p.add_run(ln), 9, mono=True)


def table(doc, header, rows):
    t = doc.add_table(rows=1, cols=len(header))
    t.style = "Table Grid"
    for i, h in enumerate(header):
        c = t.rows[0].cells[i]
        c.text = ""
        cn_font(c.paragraphs[0].add_run(h), 10, True)
    for row in rows:
        cells = t.add_row().cells
        for i, v in enumerate(row):
            cells[i].text = ""
            cn_font(cells[i].paragraphs[0].add_run(str(v)), 9.5)
    doc.add_paragraph()


def figure(doc, path, caption, width=165):
    if not os.path.exists(path):
        return
    doc.add_picture(path, width=Mm(width))
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    cn_font(p.add_run(caption), 9)


def center(doc, text, size, bold=False, space=0):
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.space_after = Pt(space)
    cn_font(p.add_run(text), size, bold)
    return p


def build(path: str):
    doc = Document()
    sec = doc.sections[0]
    sec.page_width, sec.page_height = Mm(210), Mm(297)
    sec.top_margin, sec.bottom_margin = Mm(25), Mm(22)
    sec.left_margin, sec.right_margin = Mm(25), Mm(22)

    # 页脚页码
    fp = sec.footer.paragraphs[0]
    fp.alignment = WD_ALIGN_PARAGRAPH.CENTER
    cn_font(fp.add_run("第 "), 9)
    r = fp.add_run()
    cn_font(r, 9)
    b = OxmlElement("w:fldChar"); b.set(qn("w:fldCharType"), "begin")
    it = OxmlElement("w:instrText"); it.set(qn("xml:space"), "preserve"); it.text = "PAGE"
    e = OxmlElement("w:fldChar"); e.set(qn("w:fldCharType"), "end")
    r._r.append(b); r._r.append(it); r._r.append(e)
    cn_font(fp.add_run(" 页"), 9)

    # ---------------- 封面 ----------------
    for _ in range(6):
        doc.add_paragraph()
    center(doc, "软 件 说 明 书", 28, True, 18)
    center(doc, NAME, 17, True, 10)
    center(doc, "版本号：" + VERSION, 14, False, 40)
    for _ in range(6):
        doc.add_paragraph()
    for line in ("著作权人：WuY11111", "开发完成日期：2026 年 11 月 13 日",
                 "文档版本：" + VERSION, "文档页数：约 15 页"):
        center(doc, line, 12, False, 6)
    doc.add_page_break()

    # ---------------- 目录 ----------------
    h1(doc, "目  录")
    for line in ("一、软件概述", "二、系统结构与工作原理", "三、安装与首次运行", "四、软件界面",
                 "五、控制算法说明", "六、参数说明", "七、预设曲线", "八、自动化与容错机制",
                 "九、托盘与后台运行", "十、命令行工具", "十一、常见问题", "十二、已知限制与安全说明",
                 "附录 A  配置文件字段说明", "附录 B  关键算法伪代码", "附录 C  部署与打包说明"):
        p = doc.add_paragraph()
        p.paragraph_format.space_after = Pt(4)
        cn_font(p.add_run(line), 11)
    doc.add_page_break()

    # ---------------- 一 ----------------
    h1(doc, "一、软件概述")
    h2(doc, "1.1 开发背景与目的")
    para(doc, "笔记本电脑在高负载场景（游戏、编译、渲染、跑仿真）下，受体积和散热结构限制，"
              "很容易触发温度墙而降频，表现为帧率骤降、编译变慢。常见的外部散热底座风量有限，"
              "且噪声集中在中高频，体验不佳。")
    para(doc, "另一方面，许多学生和租房用户已经拥有一台小米空气净化器：它的风机效率高、"
              "风量大、噪声低，并且可以通过局域网协议被直接控制。若用 3D 打印件把净化器出风口"
              "与笔记本进风口密闭连接，就能把它当成一个风压充足的外置散热器。")
    para(doc, "本软件解决的正是这条路线的“控制”环节：自动读取笔记本温度，按用户设定的曲线"
              "换算成净化器档位并下发，温度高时加大风量、温度低时降低风量，在散热与噪声之间取得平衡。")
    h2(doc, "1.2 适用对象")
    para(doc, "使用 Windows 笔记本电脑、并且家中或宿舍已有小米空气净化器的个人用户。"
              "软件以图形界面操作为主，使用者不需要具备编程或命令行知识。")
    h2(doc, "1.3 运行环境")
    table(doc, ["项目", "要求"], [
        ["操作系统", "Windows 10 / 11（64 位）"],
        ["处理器/显卡", "任意 x86-64 平台；有 NVIDIA 显卡时可直接读取 GPU 温度"],
        ["网络", "电脑与空气净化器处于同一局域网（同一路由器）"],
        ["受控设备", "小米空气净化器（已验证 zhimi.airpurifier.m1）"],
        ["运行方式", "免安装，解压后双击即可运行；不依赖 Python 环境"],
        ["可选组件", "LibreHardwareMonitor（用于读取真实 CPU 温度）"],
        ["磁盘占用", "约 60 MB（含运行时）"],
    ])
    h2(doc, "1.4 主要功能")
    for s in ("实时采集 CPU 温度、GPU 温度与 CPU 占用率，并在主界面以数字与曲线形式显示；",
              "将温度按可编辑折线曲线映射为净化器档位（0～16 档），支持 CPU、GPU 两条曲线；",
              "对采样值做指数平滑、对档位做回差判断、对指令做最小间隔限流，避免频繁切档；",
              "通过局域网 miIO 协议直接控制净化器，全程不经过厂商云端；",
              "支持开机自启动、系统托盘常驻、关闭主窗口后继续在后台运行；",
              "断网、无线网络掉线、设备 IP 变化时自动恢复控制；",
              "切档前自动关闭设备提示音，交还控制权时恢复原设置；",
              "内置静音、均衡、散热三套曲线预设，可一键切换；",
              "提供命令行工具，支持设备发现、状态查询、单次试算与离线自检。"):
        bullet(doc, s)

    # ---------------- 二 ----------------
    h1(doc, "二、系统结构与工作原理")
    h2(doc, "2.1 总体结构")
    para(doc, "软件采用分层结构：数据采集层负责从多种来源读取温度并按可用性自动降级；"
              "控制层负责平滑、插值、回差与限流，输出目标档位；协议层负责把档位命令编码为"
              "局域网报文并解密响应；界面层负责展示与人工干预。各层之间通过明确定义的函数接口"
              "与配置对象交互，便于单独测试。")
    figure(doc, ARCH, "图 2  系统结构与模块关系")
    h2(doc, "2.2 模块组成")
    table(doc, ["模块文件", "职责"], [
        ["app.py", "图形界面（tkinter）、托盘图标、日志显示、采样线程与引擎调度"],
        ["bridge.py", "控制核心：曲线插值、平滑、回差、限流、空闲交还；设备封装与命令行入口"],
        ["config.py", "配置默认值、字段归一化与范围校验、原子写入（先写临时文件再替换）"],
        ["sampler.py", "传感器读取与降级策略（温度服务不可用时切换为 CPU 负载曲线）"],
        ["miio_lan.py", "局域网 miIO 协议：握手、AES-128-CBC 加解密、报文校验、设备发现"],
        ["sensors.py", "温度采集实现：LHM 的 HTTP/WMI 接口、nvidia-smi、系统时间差分"],
        ["tray.py", "系统托盘图标与右键菜单（Shell_NotifyIcon）"],
        ["fake_purifier.py", "模拟设备，用于在没有真实设备时进行协议自检"],
    ])
    h2(doc, "2.3 数据流")
    para(doc, "采样线程按设定周期读取温度 → 指数平滑 → 折线插值得到目标档位 → 与设备当前档位比较，"
              "差值小于回差则不动作 → 满足最小下发间隔后，通过协议层向净化器发送档位指令 → "
              "净化器执行并回传状态 → 界面刷新显示。整个过程由独立的控制线程完成，"
              "界面线程只负责绘制，二者通过共享状态加锁访问。")
    h2(doc, "2.4 线程模型")
    table(doc, ["线程", "职责", "周期"], [
        ["界面主线程", "窗口绘制、事件响应、托盘消息处理", "事件驱动"],
        ["采样线程", "按采样周期读取温度并刷新界面数据", "2.5 秒"],
        ["控制线程", "按控制周期执行曲线计算与指令下发", "可配置，默认 10 秒"],
        ["设备线程 / 协议层", "收发 UDP 报文、加解密、超时重试", "按需"],
    ])
    h2(doc, "2.5 通信协议")
    para(doc, "软件使用小米设备公开的局域网 miIO 协议：UDP 端口 54321。报文由 32 字节报文头、"
              "AES-128-CBC 加密的 JSON 载荷和 16 字节 MD5 校验值组成，会话密钥由设备令牌经两层"
              "散列派生。首次通信通过握手报文获得设备标识，此后每条命令携带递增时间戳以防止重放。")
    para(doc, "AES 算法为本软件以纯 Python 独立实现（未使用第三方加密库），并内置 FIPS-197 与 "
              "NIST SP800-38A 的已知答案测试用例，软件启动自检时逐条验证，确保加解密结果与标准一致。")
    para(doc, "当设备 IP 地址发生变化时，软件通过局域网广播与各网段单播探测重新发现设备，"
              "并使用 MAC 地址确认设备身份，避免误连其他设备。")

    # ---------------- 三 ----------------
    h1(doc, "三、安装与首次运行")
    h2(doc, "3.1 安装步骤")
    for i, s in enumerate(("从发布页面下载压缩包，解压到任意目录（建议路径不含中文与空格）；",
                           "双击主程序启动软件，首次启动会自动在局域网内搜索可用设备；",
                           "搜索成功后自动生成配置文件，无需手工填写设备地址；",
                           "在“系统”区域勾选“开机自启”，软件会注册到当前用户的启动项；",
                           "如需读取真实 CPU 温度，安装可选组件并按 3.3 节开启。"), 1):
        para(doc, "%d. %s" % (i, s), indent=False)
    h2(doc, "3.2 手工配置")
    para(doc, "若自动搜索失败（例如净化器连接在另一台路由器上），可在程序目录下打开配置文件，"
              "手工填写设备 IP 地址与 32 位十六进制令牌，保存后重启软件。令牌可借助命令行工具的"
              "设备发现子命令获取。配置文件的读取带有容错：字段缺失或类型错误会被自动纠正为默认值，"
              "不会导致软件无法启动。")
    h2(doc, "3.3 可选组件")
    para(doc, "安装 LibreHardwareMonitor 并以管理员身份运行，在其菜单中开启远程 Web 服务后，"
              "软件即可读取真实 CPU 温度。未安装时软件自动改用 CPU 占用率估算，控制功能不受影响，"
              "界面状态栏会显示当前数据来源为“CPU 负载估算”。")
    h2(doc, "3.4 卸载")
    para(doc, "软件为绿色免安装形式，取消“开机自启”后直接删除程序目录即可完成卸载；"
              "配置、状态与日志均位于程序目录内，不会写入注册表之外的系统位置。")

    # ---------------- 四 ----------------
    h1(doc, "四、软件界面")
    h2(doc, "4.1 主界面")
    figure(doc, SHOT, "图 1  软件主界面")
    h2(doc, "4.2 区域说明")
    table(doc, ["区域", "功能说明"], [
        ["顶部状态条", "显示控制状态、温度数据来源状态、设备连接状态"],
        ["左上信息区", "实时 CPU/GPU 数值、当前档位与转速、空气质量指数、目标档位"],
        ["右上曲线区", "温度—档位曲线编辑器，可拖动、删除、新增折点，支持 CPU/GPU 切换"],
        ["左中参数区", "采样周期、最小间隔、档位上下限、回差、空闲交还、生效时段、交还方式"],
        ["左下系统区", "启动温度服务、开机自启、打开配置目录、静音开关"],
        ["底部控制区", "开始控制、暂停、交还自动、试档按钮、实时日志窗口"],
    ])
    h2(doc, "4.3 曲线编辑操作")
    para(doc, "在曲线区按住折点圆点即可拖动，横轴为温度（30～100℃），纵轴为档位（0～16 档），"
              "松开鼠标后配置自动保存并立即生效；右键点击圆点可删除该折点；“加点”按钮在当前"
              "曲线上插入新折点；“恢复默认曲线”可恢复出厂曲线。曲线下方的三个按钮可一键套用"
              "静音、均衡、散热三套预设。")
    h2(doc, "4.4 日志窗口")
    para(doc, "底部日志按时间顺序显示采样值、目标档位、指令下发结果与异常信息，最多保留最近"
              "数百条，并同步写入程序目录下的滚动日志文件，便于事后排查。")

    # ---------------- 五 ----------------
    h1(doc, "五、控制算法说明")
    h2(doc, "5.1 控制流程")
    figure(doc, FLOW, "图 3  单轮控制流程")
    h2(doc, "5.2 折线插值")
    para(doc, "曲线由若干折点（温度，档位）组成，按温度所在区间线性插值得到目标档位；温度低于"
              "首个折点时取首个档位，高于末个折点时取末个档位，因此曲线两端天然具有限幅作用。"
              "CPU 与 GPU 两条曲线分别计算，取两者较大值作为最终目标档位。")
    h2(doc, "5.3 指数平滑")
    para(doc, "为避免瞬时尖峰造成档位跳动，对采样值做指数移动平均：新值 = 旧值 + 系数 ×（采样值 − 旧值）。"
              "平滑系数默认 0.35，系数越小曲线越平滑、响应越慢，可在参数区调整。")
    h2(doc, "5.4 回差控制")
    para(doc, "目标档位与设备当前档位的差值小于回差（默认 2 档）时不发送指令。实测回差为 1 时，"
              "在 62～68℃ 区间会出现每 30 秒一次的档位抖动，因此默认取 2，兼顾稳定与响应。")
    h2(doc, "5.5 指令限流")
    para(doc, "两条控制指令之间强制间隔不小于设定值（默认 30 秒），保护风机电机并避免噪声突变；"
              "该间隔与采样周期相互独立，可通过参数分别调整。")
    h2(doc, "5.6 空闲交还")
    para(doc, "连续设定的时间（默认 10 分钟）内目标档位均低于曲线下限时，判定电脑处于空闲状态，"
              "自动把设备控制权交还米家自动模式（也可配置为关机或保持），避免设备长时间空转。")
    h2(doc, "5.7 降级策略")
    para(doc, "当温度服务不可用时，CPU 通道改用“CPU 占用率—档位”的备用曲线。备用曲线与温度曲线"
              "相互独立，因此降级过程不会造成档位突变；界面会同步标注数据来源，便于用户判断。")
    h2(doc, "5.8 决策示例")
    table(doc, ["轮次", "采样温度", "平滑后", "曲线目标", "当前档位", "动作"], [
        ["1", "52℃", "52.0℃", "0 档", "0 档", "差值 0 < 2，不下发"],
        ["2", "61℃", "55.2℃", "1 档", "0 档", "差值 1 < 2，不下发"],
        ["3", "70℃", "60.5℃", "3 档", "0 档", "差值 3 ≥ 2，且满足限流，下发 3 档"],
        ["4", "66℃", "62.3℃", "3 档", "3 档", "差值 0 < 2，不下发"],
        ["5", "58℃", "59.4℃", "2 档", "3 档", "差值 1 < 2，不下发"],
    ])

    # ---------------- 六 ----------------
    h1(doc, "六、参数说明")
    table(doc, ["参数", "默认值", "取值范围", "说明"], [
        ["采样周期", "10 秒", "2～60 秒", "两次采样的时间间隔"],
        ["最小间隔", "30 秒", "0～600 秒", "两条控制指令之间的最小时间间隔"],
        ["最低档", "0", "0～17", "档位下限，0 表示停转"],
        ["最高档", "16", "0～17", "档位上限"],
        ["回差", "2", "1～8", "目标与当前档位差值小于该值时不动作"],
        ["空闲交还", "10 分钟", "0～120 分钟", "低于曲线下限持续该时间后交还，0 表示从不"],
        ["平滑系数", "0.35", "0.05～1.0", "指数移动平均系数"],
        ["生效时段", "空", "HH:MM-HH:MM", "仅在该时段内控制，支持跨午夜"],
        ["交还方式", "auto", "auto/off/keep", "交还时自动模式、关机或保持"],
    ])

    # ---------------- 七 ----------------
    h1(doc, "七、预设曲线")
    para(doc, "软件内置三套曲线预设，覆盖对噪声敏感的夜间使用、日常均衡使用与高负载散热三种场景。"
              "可在主界面曲线区下方按钮或系统托盘右键菜单中一键切换，切换后立即保存并生效。")
    table(doc, ["预设", "CPU 曲线（温度→档位）", "GPU 曲线（温度→档位）"], [
        ["静音", "55→0，65→2，75→4，85→6，95→9",
         "50→0，58→2，66→5，74→8，82→11"],
        ["均衡", "52→0，60→2，68→4，76→7，83→10，88→13，94→16",
         "45→0，52→2，58→4，64→7，70→10，76→13，82→16"],
        ["散热", "45→0，55→3，62→6，68→9，75→12，82→16",
         "40→0，48→3，55→6，62→9，68→12，75→16"],
    ])
    para(doc, "上述曲线基于实测标定：处理器空载约 48～51℃，全核满载 94～96℃，偶发尖峰可达 70℃ 以上；"
              "显卡空载 48～52℃，满载 75～85℃。档位与转速的对应关系亦经实测：1 档约 700 转/分，"
              "4 档约 850 转/分，8 档约 1400 转/分，16 档约 2100 转/分，即低档位区间转速变化平缓，"
              "适合作为日常静音区间。")

    # ---------------- 八 ----------------
    h1(doc, "八、自动化与容错机制")
    table(doc, ["异常场景", "软件行为"], [
        ["无线网络掉线或切换", "按预设的无线网络名称自动重连，成功后立即恢复控制"],
        ["设备 IP 地址变化", "连接失败时自动重新发现设备，按 MAC 确认后更新配置"],
        ["持续无法连接设备", "采用指数退避重试（20 秒至 300 秒），避免无效请求与日志刷屏"],
        ["温度服务不可用", "自动降级为 CPU 占用率估算，界面标注当前数据来源"],
        ["净化器断电", "下次连接成功后自动开机并恢复接管"],
        ["程序异常退出", "配置与运行状态已持久化，重启后按上次状态继续控制"],
        ["设备掉线超过 5 分钟", "通过系统托盘气泡提示用户检查电源与网络"],
    ])
    para(doc, "上述机制均在不打断用户使用的前提下自动完成，正常情况下用户无需人工干预。")

    # ---------------- 九 ----------------
    h1(doc, "九、托盘与后台运行")
    para(doc, "关闭主窗口时软件不会退出，而是缩小到系统托盘继续控制，避免误关导致散热中断。"
              "托盘图标悬停可查看实时状态（CPU、GPU、当前档位、运行状态），双击打开主窗口；"
              "右键菜单提供“显示窗口”“暂停 / 继续控制”“预设曲线”“交还自动并退出”等命令。")
    para(doc, "软件启动时可通过命令行参数以静默方式运行，只保留托盘图标，适合作为开机自启动项。")

    # ---------------- 十 ----------------
    h1(doc, "十、命令行工具")
    table(doc, ["命令", "作用"], [
        ["discover", "扫描局域网内的设备，输出地址、令牌与型号"],
        ["probe", "列出本机可读取的温度测点"],
        ["status", "查询净化器当前状态（开关、模式、档位、转速、空气质量）"],
        ["set N", "手动设置档位"],
        ["once --dry-run", "按当前温度试算一次档位，不下发设备"],
        ["run", "以命令行方式常驻运行"],
        ["selftest", "使用内置模拟设备执行协议与算法自检，不需要真实设备"],
    ])
    para(doc, "命令行工具与图形界面共用同一套控制核心与配置，二者可以相互替代使用。")

    # ---------------- 十一 ----------------
    h1(doc, "十一、常见问题")
    for q, a in (
        ("软件找不到净化器怎么办？",
         "确认电脑与净化器连接在同一路由器下（电脑用网线上网时，需另外连接该路由器的无线网络）；"
         "可用命令行 discover 子命令检查；若仍找不到，请在配置文件中手工填写地址与令牌。"),
        ("CPU 为什么显示百分比而不是温度？",
         "表示未检测到温度服务，软件已自动降级为 CPU 占用率估算。安装并以管理员身份运行"
         "LibreHardwareMonitor、开启远程 Web 服务后，软件会自动恢复温度显示，无需重启配置。"),
        ("为什么档位不是一直变化？",
         "软件带有回差与指令限流机制，小幅温度波动不会触发调档，这是为了避免噪音与设备损耗，"
         "属于正常现象。"),
        ("切换档位时会有提示音吗？",
         "默认开启“静音”，软件在切档前关闭设备提示音，交还控制权时恢复原设置。"),
        ("关闭窗口后还会继续控制吗？",
         "会。关闭窗口只是隐藏界面，控制线程仍在托盘中运行。"),
        ("可以同时控制多台设备吗？",
         "当前版本针对单台设备设计，暂不支持多设备。"),
    ):
        h2(doc, q)
        para(doc, a)

    # ---------------- 十二 ----------------
    h1(doc, "十二、已知限制与安全说明")
    for s in ("当前仅验证小米空气净化器 m1/m2 系列，其他型号的档位指令可能不同；",
              "控制方式为温度到档位的曲线映射，不是恒温闭环控制；",
              "Windows 读取真实 CPU 温度需要管理员权限的第三方内核驱动，无法完全绕开；",
              "软件不含任何联网上传功能，全部通信仅发生在本地局域网内，不涉及账号与云端；",
              "配置文件中保存的设备令牌等同于该设备的控制权限，请勿外传或提交到公开仓库。"):
        bullet(doc, s)

    # ---------------- 附录 A ----------------
    doc.add_page_break()
    h1(doc, "附录 A  配置文件字段说明")
    para(doc, "配置文件采用 JSON 格式，位于程序目录下。软件读取时会自动补齐缺失字段并校验取值范围，"
              "字段错误不会导致软件无法启动。", indent=True)
    table(doc, ["字段", "类型", "默认值", "说明"], [
        ["purifier.ip", "字符串", "空", "设备局域网地址"],
        ["purifier.token", "字符串", "空", "32 位十六进制设备令牌"],
        ["purifier.timeout_s", "数字", "4", "单次通信超时（秒）"],
        ["purifier.silent", "布尔", "true", "切档前是否关闭设备提示音"],
        ["purifier.wifi_profile", "字符串", "空", "断网时自动重连的无线网络名称"],
        ["control.interval_s", "数字", "10", "控制周期（秒）"],
        ["control.min_level", "整数", "0", "档位下限"],
        ["control.max_level", "整数", "16", "档位上限"],
        ["control.deadband", "整数", "2", "回差档数"],
        ["control.min_command_gap_s", "数字", "30", "两条指令的最小间隔（秒）"],
        ["control.ema_alpha", "数字", "0.35", "指数平滑系数"],
        ["control.idle_release_min", "数字", "10", "空闲交还时间（分钟）"],
        ["control.idle_action", "字符串", "auto", "交还方式：auto/off/keep"],
        ["control.active_hours", "数组", "空", "生效时段列表"],
        ["control.restore_on_exit", "布尔", "true", "退出时是否交还设备"],
        ["sensors[].name", "字符串", "cpu/gpu", "通道名称"],
        ["sensors[].kind", "字符串", "lhm/nvidia", "数据来源类型"],
        ["sensors[].match", "数组", "见默认配置", "测点名称匹配关键字"],
        ["sensors[].curve", "二维数组", "见默认配置", "温度—档位折点列表"],
        ["config_version", "整数", "1", "配置结构版本号，用于后续升级迁移"],
    ])

    # ---------------- 附录 B ----------------
    h1(doc, "附录 B  关键算法伪代码")
    h2(doc, "B.1 折线插值")
    code(doc, [
        "function curve_value(points, x):",
        "    pts = sort(points by temperature)",
        "    if x <= pts.first.temp:  return pts.first.level",
        "    if x >= pts.last.temp:   return pts.last.level",
        "    for (p0, p1) in consecutive_pairs(pts):",
        "        if p0.temp <= x <= p1.temp:",
        "            return p0.level + (p1.level - p0.level) * (x - p0.temp) / (p1.temp - p0.temp)",
    ])
    h2(doc, "B.2 单轮控制")
    code(doc, [
        "function control_step():",
        "    samples = read_sensors()                      # 失败则降级到负载曲线",
        "    level = max(curve_value(cpu_curve, cpu), curve_value(gpu_curve, gpu))",
        "    level = clamp(level, control.min_level, control.max_level)",
        "    if abs(level - device.level) < control.deadband:",
        "        return                                    # 死区内不动作",
        "    if now - last_command_at < control.min_command_gap_s:",
        "        return                                    # 限流",
        "    if level != device.level:",
        "        send_set_mode('favorite')",
        "        send_set_level(level)",
        "    last_command_at = now",
    ])
    h2(doc, "B.3 指数平滑")
    code(doc, [
        "function ema(previous, sample, alpha):",
        "    if previous is null: return sample",
        "    return previous + alpha * (sample - previous)",
    ])

    # ---------------- 附录 C ----------------
    h1(doc, "附录 C  部署与打包说明")
    para(doc, "源码采用 Python 3.10 以上版本编写，仅使用标准库，不依赖任何第三方包。"
              "发布时使用打包工具将其编译为单个可执行文件加运行时目录的形式，用户无需安装 Python。")
    table(doc, ["目录/文件", "说明"], [
        ["散热管家.exe", "主程序"],
        ["_internal/", "Python 运行时与标准库（打包生成）"],
        ["config.json", "配置文件（首次运行自动生成）"],
        ["state.json", "运行状态，用于重启后恢复"],
        ["logs/", "滚动日志目录，单文件上限 2 MB，保留 3 份"],
    ])
    para(doc, "自动化构建脚本与持续集成配置随源码一同提供：每次提交会在 Windows 环境下自动执行"
              "静态检查与协议自检，确保发布版本可用。")

    # ---------------- 附录 D ----------------
    h1(doc, "附录 D  实测标定数据")
    h2(doc, "D.1 档位与转速对应关系")
    para(doc, "在测试机型（小米空气净化器 2）上逐档设置并读取设备回传转速，结果如下表。"
              "可见 0～4 档处于低转速平台，档位提高而转速变化很小，适合作为夜间静音区间；"
              "6 档以上风量随档位明显上升，用于高负载散热。")
    table(doc, ["档位", "转速（转/分）", "主观听感", "建议场景"], [
        ["1", "700", "几乎无声", "夜间轻度使用"],
        ["2", "744", "几乎无声", "日常办公"],
        ["3", "799", "轻微风噪", "日常办公 / 网页浏览"],
        ["4", "851", "轻微风噪", "多任务处理"],
        ["6", "1171", "明显风噪", "游戏 / 编译"],
        ["8", "1401", "明显风噪", "高负载游戏"],
        ["10", "1700", "较响", "长时间高负载"],
        ["12", "1903", "较响", "渲染 / 仿真"],
        ["14", "1995", "很响", "短时极限负载"],
        ["16", "2099", "很响", "短时极限负载"],
    ])
    h2(doc, "D.2 散热效果对比")
    para(doc, "在同一台笔记本上运行满载压力测试，分别将净化器固定在 0 档（不吹风）与 16 档，"
              "记录处理器温度稳定值：0 档时约 94.2℃，16 档时约 92.2℃，温差约 2℃。"
              "该结果表明：净化器在此方案中属于“辅助散热”，主要收益在于降低风扇高转速带来的"
              "机身噪音与表面温度，而非大幅压低核心温度；因此软件把控制重点放在"
              "“按需送风、闲时安静”，而不是一味拉满档位。")

    # ---------------- 附录 E ----------------
    h1(doc, "附录 E  故障排查对照表")
    table(doc, ["现象", "可能原因", "处理方式"], [
        ["界面显示设备未连接", "设备断电、IP 变化、与电脑不在同一网段",
         "检查电源与网络；软件会在数分钟内自动重试；必要时手工更新配置"],
        ["CPU 显示为百分比", "温度服务未运行或未开启远程接口",
         "以管理员身份启动温度服务并开启远程 Web 服务"],
        ["档位长时间不变", "温度处于同一曲线区间，或处于回差范围内",
         "查看日志中的目标档位；如需更灵敏可减小回差"],
        ["切档有提示音", "静音选项被关闭，或设备端设置被重置",
         "在系统区域重新勾选“静音”"],
        ["软件启动后立即退出", "配置文件损坏或端口被占用", "删除配置文件让软件重新生成；检查端口占用"],
        ["开机不自启", "启动项被安全软件拦截", "在系统中重新授权，或把程序目录加入白名单"],
    ])

    # ---------------- 附录 F ----------------
    h1(doc, "附录 F  版本历史")
    table(doc, ["版本", "日期", "主要内容"], [
        ["V1.0", "2026-11-13", "首个正式版本：曲线控制、平滑与回差、托盘常驻、开机自启、"
                              "断网自愈、静音切档、三套预设曲线、命令行工具与离线自检"],
    ])

    doc.save(path)
    return path


if __name__ == "__main__":
    out = os.path.join(BASE, "docs", "软著-说明书.docx")
    build(out)
    print("已生成:", out)
