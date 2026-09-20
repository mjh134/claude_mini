"""输出层 —— 所有给"人"看的东西从这里走,别再各文件自己 print。

## 分两层,别混

    **渲染**(下面那一堆 assistant / tool_call / status ...)
        纯函数,谁都能调,包括子agent的线程。rich 的 Console.print 自带锁
        (内部 `with self`,RLock),所以从几条线程同时打不会把输出撕开。

    **反问**(ask_sync)
        只有一个调用点:HOOKS.permission_hook。它和渲染完全不是一回事 ——
        它要**从终端把控制权抢回来**问一句话,而这个改造之后终端的所有权在
        读线程手上(prompt_toolkit)。细节见 ask_sync 的注释。

## 为什么要一个模块

之前 12 个文件、67 处 print 各写各的,同一个东西("开始跑工具了")在不同地方
长得不一样,层级也表达不出来。现在按**颜色深浅 + 缩进**分三级,不用框:

    对话级   亮     assistant / tool_call / error
    状态级   dim    status
    调试级   最暗   debug —— 默认**整个不显示**,UI_VERBOSE=1 才开

宽窄靠 `_W()` 自适应,窄终端下把长行折了,别撑破。

自检:python ui.py
"""
import os
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass

from rich.cells import cell_len
from rich.console import Console
from rich.text import Text

#---------------------------------------------------------------------------
#★ 屏幕上那个 `?[2;3m` 是怎么来的(2026-09-20 定案)
#---------------------------------------------------------------------------
#**patch_stdout() 默认会把每个 ESC 换成 `?`** —— 看 prompt_toolkit 的
#`output/vt100.py::Vt100_Output.write()`:
#
#       self._buffer.append(data.replace("\x1b", "?"))
#
#patch_stdout(raw=False) 走的就是这个 write();raw=True 走 write_raw(),原样放行。
#所以 `?[2;3m` 里的 `?` 不是"复制时丢了转义",就是**被它替换出来的**。
#修法就一个字:`patch_stdout(raw=True)`(见下面 read_input)。
#
#**这个坑我绕了很远,记录一下别再重走**:我先是认定"控制台不认 ANSI / prompt_toolkit
#选了不解析转义码的输出对象",去查 is_win_vt100_enabled、Win32Output、rich 的
#legacy_windows、color_system…… 全是错的方向。真相是**输出层从来没坏**,
#是 prompt_toolkit **故意**把那串字节替换掉,免得别人的 print 弄花提示符。
#教训:遇到"某个字节在屏幕上变成另一个字符",先怀疑**有人替换了它**,
#再怀疑"渲染器不认识它" —— 后者要费劲得多,而前者一行就能验
#(`grep -n 'replace' 那个库的输出层`)。
#
#下面这几行保持最朴素的写法:不要在这里自己开 VT、也不要按"输出对象认不认 ANSI"
#去关 rich 的颜色 —— 那些都是我照错误方向加的东西,已撤掉(raw=True 之后它们既没用,
#还会在启动时改控制台模式)
# 文件名从 rich 的语义里拿,不重名
console = Console()

# 调试级开关。开:UI_VERBOSE=1 python main.py
#**运行时可改** —— /折叠 关 就是把它置 True(见 run_command)。
#能改的前提是**没人 `from ui import UI_VERBOSE`**:那样会拷走一份值,
#之后改这里也影响不到那个副本。全项目扫过,一个都没有,都走的 ui.XXX
UI_VERBOSE = os.environ.get("UI_VERBOSE", "") not in ("", "0", "false", "False")


#---------------------------------------------------------------------------
#折叠登记处:屏幕上只出一行,全文存这儿,想看再 /展开 调出来
#---------------------------------------------------------------------------
#**为什么是"重打一遍",不是"原地展开"**(2026-09-20 定案):
#终端的历史区**只能追加,改不了**。鼠标事件也只在应用程序接管整屏时才上报,
#点已经滚上去的行,终端只会理解成"选中文本"。真要做原地展开,就得把整个 REPL
#换成全屏 TUI —— 那要丢掉原生 scrollback、复制粘贴变麻烦、patch_stdout 整套重做,
#代价远大于收益。所以这里选了另一条:**默认只出一行摘要,全文留一份,展开时重打**。
#信息一样拿得到,只多敲一条命令。
#
#**多线程**:子agent、团队成员、后台任务都在各自线程里调 ui.*(见模块开头),
#所以登记必须加锁。**序号在锁里分配** —— 两条线程同时登记却拿到同一个号的话,
#用户 /展开 那个号就会开到别人的内容上,而那正是这个功能的全部意义
FOLD_KEEP = 200          #只留最近这些条。全文可能很大,不设上限就是内存泄漏
EXPAND_MAX_CHARS = 20000 #单次展开的显示上限,超了截断并说明 —— 别让一条结果刷掉整屏

_FOLD_LOCK = threading.Lock()
_FOLD = deque(maxlen=FOLD_KEEP)   #元素是 Folded,按登记顺序
_FOLD_SEQ = 0                     #只增不减的绝对序号,用户看到的就是它


@dataclass
class Folded:
    """一条被折叠掉的内容。n 是它的句柄(/展开 n 用)"""
    n: int
    kind: str        #思考 / 工具 / 结果 / 子agent
    who: str         #谁打的;空 = 主agent
    title: str       #折叠行上那句摘要,展开时当标题复用
    body: str        #全文,展开时原样重打


def _fold(kind, who, title, body):
    """登记一条可展开的内容,返回它(空内容返回 None)。

    **空的不登**:登了展开出来也是空白,只会让序号白涨、把 /折叠列表 灌满噪音
    """
    global _FOLD_SEQ
    body = "" if body is None else str(body)
    if not body.strip():
        return None
    with _FOLD_LOCK:
        _FOLD_SEQ += 1
        item = Folded(n=_FOLD_SEQ, kind=kind, who=who or "",
                      title=str(title), body=body)
        _FOLD.append(item)
    return item


def fold_items():
    """当前还留着的折叠项(副本,调用方拿不到内部 deque)"""
    with _FOLD_LOCK:
        return list(_FOLD)


def fold_seq():
    """至今登记过多少条(含已经被挤掉的)—— /折叠列表 用它说清"只列得出最近这些\""""
    with _FOLD_LOCK:
        return _FOLD_SEQ


def _body_lines(body):
    """展开时正文的缩进 + 封顶"""
    s = str(body)
    cut = ""
    if len(s) > EXPAND_MAX_CHARS:
        cut = f"\n    …(共 {len(s)} 字,这里只显示前 {EXPAND_MAX_CHARS} 字)"
        s = s[:EXPAND_MAX_CHARS]
    return "\n".join("    " + ln for ln in s.splitlines()) + cut


def expand(n=None):
    """把某条折叠的内容重打一遍。n 省略 = 最近一条"""
    items = fold_items()
    if not items:
        console.print(Text("  (还没有可展开的内容)", style="dim"))
        return
    if n is None:
        item = items[-1]
    else:
        item = next((x for x in items if x.n == n), None)
        if item is None:
            lo, hi = items[0].n, items[-1].n
            console.print(Text(f"  (没有 #{n}。现在留着的范围是 #{lo}–#{hi},"
                               f"敲 /折叠列表 看看)", style="dim"))
            return

    #**上下各一条细线**:展开是重打进滚动日志里的,没有边界就分不清到哪结束
    head = f"  ↳ #{item.n} {item.kind}"
    if item.who:
        head += f" [{item.who}]"
    if item.title:
        #标题同理会超宽,一样截断 —— 边界线夹着的块里混进一条折行很难看
        head += "  " + one_line(item.title, max(10, _W() - cell_len(head) - 2))
    rule()
    console.print(Text(head, style="bold"))
    #思考是"过程",暗一点;工具参数和结果是"数据",要能读
    console.print(Text(_body_lines(item.body),
                       style="dim" if item.kind == "思考" else ""))
    rule()


def fold_list():
    """列出还能展开的项。只列最近一段 —— 序号是绝对的,但 200 条全列出来没人看"""
    items = fold_items()
    if not items:
        console.print(Text("  (还没有可展开的内容)", style="dim"))
        return
    total = fold_seq()
    dropped = total - len(items)
    tail = f",更早的 {dropped} 条已被挤掉" if dropped else ""
    show = items[-15:]
    console.print(Text(f"  · 可展开 {len(items)} 条(共登记过 {total} 条{tail}),"
                       f"列最近的:", style="dim"))
    for x in show:
        #**一行一条,超了截断**。不截的话标题一长就被 rich 折行,
        #折出来的第二行没有缩进,整个清单就散了(而且看起来像"标题里有换行")
        #kind 列宽 8:最长的类型是「子agent」(7 格),按 6 补会补不上、标题直接贴上去
        head = ("      " + pad_cells(f"#{x.n}", 6) + pad_cells(x.kind, 8)
                + (f"[{x.who}] " if x.who else ""))
        console.print(Text(head + one_line(x.title, max(10, _W() - cell_len(head))),
                           style="dim"))
    console.print(Text("  · /展开 <序号> 看全文;/展开 就是最近一条", style="dim"))


def fold_set(on):
    """切折叠开关。on=True 折叠(默认),on=False 全展开(等于 UI_VERBOSE=1)"""
    global UI_VERBOSE
    UI_VERBOSE = not on
    console.print(Text("  · 折叠:" + ("开(思考和全文只出一行摘要)" if on
                                     else "关(思考、全文直接打出来)"), style="dim"))


#---------------------------------------------------------------------------
#斜杠命令:只认领下面这几条,**其余一律放行**
#---------------------------------------------------------------------------
#★ 不认识的 /xxx 必须返回 False 交给模型。这个项目里 `/ask` 之类**本来就是
#直接当文本发下去的**(没有命令层),加了这个层以后要是"凡 / 开头都吞掉",
#那些用法会**静默失效** —— 用户看不出区别,只会觉得模型突然不认命令了。
#所以这里是**白名单**:认领才拦,认不出就原样放行,和加这一层之前一模一样。
#
#放在 ui.py 而不是 main.py:命令的语义(序号、折叠状态)全在折叠登记处,
#分开写就得把内部结构暴露出去;main.py 的循环还挂着"三个唤醒来源"的不变量注释,
#不适合再塞一张命令表
#(用法, 别名, 说明) —— 别名单列一栏,不放说明里:放说明里就等于"藏起来",
#用户敲 /帮助 本来就是为了**发现**有什么能敲,藏在句子中间的等于没写。
#★ 这张表必须列全,包括 /帮助 自己:不然敲了 /帮助 也看不出 /帮助 存在
_COMMANDS = (
    ("/展开 [序号]", "/e", "重打某条折叠内容的全文;不给序号 = 最近一条"),
    ("/折叠列表", "", "列出还能展开的项和它们的序号"),
    ("/折叠 [开|关]", "", "切折叠开关;关 = 思考和全文直接打出来"),
    ("/帮助", "/? /help", "就是这张表"),
)


def run_command(line):
    """处理一条斜杠命令。**认领了返回 True,认不出返回 False(交给模型)**"""
    parts = str(line).strip().split()
    if not parts:
        return False
    cmd, rest = parts[0], parts[1:]

    if cmd in ("/展开", "/e"):
        if not rest:
            expand()
            return True
        try:
            expand(int(rest[0]))
        except ValueError:
            console.print(Text(f"  (序号要是个数字,给的是 {rest[0]!r};"
                               f"敲 /折叠列表 看看有哪些)", style="dim"))
        return True

    if cmd == "/折叠列表":
        fold_list()
        return True

    if cmd == "/折叠":
        if not rest:
            console.print(Text("  · 折叠现在是:" + ("开" if not UI_VERBOSE else "关")
                               + "(敲 /折叠 开 或 /折叠 关)", style="dim"))
            return True
        arg = rest[0]
        if arg in ("开", "on", "1", "true"):
            fold_set(True)
        elif arg in ("关", "off", "0", "false"):
            fold_set(False)
        else:
            console.print(Text(f"  (只认 开/关,给的是 {arg!r})", style="dim"))
        return True

    if cmd in ("/帮助", "/?", "/help"):
        console.print(Text("  · 斜杠命令(别的 /xxx 会原样发给模型):", style="dim"))
        #列宽按**显示宽度**算,不用 f"{s:<14}" —— 那个按字符数补,
        #而「/展开」是 3 个字符 / 5 个格,补出来的列会比纯 ASCII 的窄
        w1 = max(cell_len(u) for u, _, _ in _COMMANDS)
        w2 = max(cell_len(a) for _, a, _ in _COMMANDS)
        for usage, alias, desc in _COMMANDS:
            console.print(Text("      " + pad_cells(usage, w1 + 2)
                               + pad_cells(alias, w2 + 2) + desc, style="dim"))
        return True

    return False


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------
def rule():
    """一条细线。换"大段"的时候用,别在两行之间乱插"""
    console.print(Text("─" * _W(), style="grey35"))


def _W():
    """当前终端宽度,留一点余量。拿不到就当 80"""
    try:
        w = console.width
    except Exception:
        return 80
    return max(20, min(w, 100) - 1)


#用户那一行的竖条**不在渲染层** —— 它就是 prompt_toolkit 的提示符本身。
#在真终端上,你敲的那一行就是 `▌ 你好`,回车后留在屏幕上,不需要再打一遍。
#
#以前这里是 turn_user(),一个打印 `▌ xxx` 的渲染函数。但 main.py **从来没调过它**
#(实测:全项目只有 ui.py 自己的演示块在用),于是用户说的话在屏幕上只剩
#prompt_toolkit 那句英文 "User: " —— 设计里的标记一次都没出现过。
#补一次调用不行:敲完回车,同一句话会连着显示两遍。让提示符带标记才是对的。
#
#**故意存成字符串**:prompt_toolkit 的导入是懒的(见 read_input),模块级去 import 它
#会把 ui.py 的导入面撑大 —— 而 llm.py 也 import ui,那条链不该被拖进来
PROMPT_ANSI = "\x1b[1;35m▌ \x1b[0m"


def assistant(md):
    """助手正文。默认当 markdown 渲染 —— 模型本来就爱出 markdown,
    以前是原样吐出来,星号全糊在屏幕上"""
    console.print(Text("  助手", style="bold green"))
    console.print(_md(md))


def _md(md):
    """markdown 渲染,但**只在真终端上**。管道/重定向时 rich 会把颜色码灌进文件,
    日志就没法看了 —— 那种情况直接出原文"""
    if not sys.stdout.isatty():
        return Text(str(md))
    from rich.markdown import Markdown
    try:
        return Markdown(str(md))
    except Exception:
        return Text(str(md))


def thinking(text, lines=None):
    """思考。默认折成一行,**想看再 /展开**。

    折叠而不是整段打出来,是因为思考经常比正文还长;但它又必须**可见**
    (模型在想什么,是判断它跑没跑偏的主要依据),所以留一行摘要。
    全文进折叠登记处 —— 这条 docstring 以前就写着"想看再展开",但那条路当时
    并不存在(只能 UI_VERBOSE=1 重开进程),现在补上了
    """
    if text is None:
        return
    n = lines if lines is not None else str(text).count("\n") + 1
    _fold("思考", "", f"思考 {n} 行", text)
    t = Text("  › 思考 ", style="dim italic")
    t.append(f"{n} 行", style="dim")
    console.print(t)
    if UI_VERBOSE:
        console.print(Text(_clip(str(text)), style="dim"))


def _clip(s, limit=None):
    """缩进 + 截断。超长的东西(工具结果、思考)不能整个糊上来"""
    limit = limit or _W() * 6
    s = str(s)
    if len(s) > limit:
        s = s[:limit] + f"…(还有 {len(s) - limit} 字)"
    return "\n".join("    " + ln for ln in s.splitlines())


def clip_cells(s, cells):
    """按**显示宽度**截断,不是按字符数。

    ★ 中文和 emoji 是双宽。按 len() 截出来的"55 字",打到终端上能占 100 多列 ——
    rich 换行是按显示宽度算的,于是那一行被折断,下一行的缩进就散了
    (实测:一行权限拒绝语把 `✓` 那行的缩进整个顶掉)。宽度只能用 cell_len 数。
    """
    out, used = [], 0
    for ch in str(s):
        w = cell_len(ch)
        if used + w > cells:
            return "".join(out) + "…"
        out.append(ch)
        used += w
    return str(s)


def pad_cells(s, cells):
    """右侧补空格到指定**显示宽度**。

    `f"{s:<6}"` 是按**字符数**补的,而中文是双宽 —— 拿它排出来的列会歪
    (``结果`` 是 2 个字符 / 4 个格,按 6 个字符补完还是比 ``bash`` 窄)。
    和 clip_cells 是一对:一个截一个补,都只能数 cell_len
    """
    s = str(s)
    return s + " " * max(0, cells - cell_len(s))


def one_line(text, limit=None):
    """压成一行再按显示宽度截断 —— 给"摘要"用。

    工具参数、子任务prompt、结果摘要都走它:这些东西的真实长度没有上限,
    直接拼进状态行会把屏幕撑破
    """
    s = " ".join(str(text).split())
    return clip_cells(s, limit or max(20, _W() - 26))


def tool_call(name, args="", who=None, full=None):
    """工具调用。**这一行以前根本没有** —— claude.py 里那句 print 是注释掉的,
    工具跑起来在屏幕上完全不可见,只能干等。这是这次改造里最实的一块。

    who:谁在调。主agent不传(它就是默认那个),团队成员/后台子agent传自己的 id ——
    它们各在自己的线程里跑,不标出来就分不清这行是谁打的

    full:未压缩的完整参数(调用方给 block.input)。屏幕上那行只出**代表参数**、
    而且 content 还被截到 40 字,所以整段参数只有 /展开 才看得到

    没有耗时参数:调用的耗时要等工具**回来**才知道,量在那儿(tool_result)。
    两头都报一遍的话,同一段时间会在屏幕上出现两次

    宽度是**先量两头再分中间**:前缀(who + 工具名)和后缀都是定长的、都要保住,
    能伸缩的只有中间那段参数。先截参数再拼尾巴会超宽 —— 超了 rich 就折行,
    折出来的第二行没有缩进,一整块就散了
    """
    who_txt = f"[{who}] " if who else ""
    a = str(args).replace("\n", " ⏎ ")
    room = _W() - 4 - cell_len(who_txt) - cell_len(str(name)) - 2
    shown = clip_cells(a, room) if (a and room > 6) else ""
    _fold("工具", who, f"{name} {shown}".strip(), full)

    t = Text("  ⚙ ", style="bright_cyan")
    if who:
        t.append(who_txt, style="dim cyan")
    t.append(str(name), style="bold bright_cyan")
    if shown:
        t.append("  " + shown, style="dim")
    console.print(t)


def tool_result(brief, ms=None, who=None):
    """工具回来的那一行。

    标记按结果自己选:`✓` 只在工具真的成功时出现。工具错误在这个项目里一律以
    `❌` 开头(claude.py / HOOKS.py / task_runner.py 都这么写),所以这里认这个前缀 ——
    不然一条"权限被拒"会显示成 `✓ ❌ ...`,一个对勾配一个错误,读起来是"成功了"。

    brief 要短 —— 完整结果该进 tool_result/ 文件,不在这刷屏。但**必须有一行**:
    只报"开始调"却等不到"回来了",看起来和卡住一模一样

    进来的是**未截断的完整结果**(claude.py 传的是 output 原文,截断发生在本函数里),
    所以顺带把全文登记进折叠处 —— 屏幕上那行是压成一句的,/展开 才看得到原文
    """
    if brief is None or brief == "":
        brief = "(无输出)"
    brief = str(brief).lstrip()
    failed = brief.startswith("❌")
    if failed:
        brief = brief[1:].lstrip()      #❌ 已经在标记位上占过了,别重复
    who_txt = f"[{who}] " if who else ""
    tail = f"   {ms}ms" if ms is not None else ""
    shown = one_line(brief, max(10, _W() - 6 - cell_len(who_txt) - cell_len(tail)))
    _fold("结果", who, ("✗ " if failed else "") + shown, brief)

    t = Text("    ✗ " if failed else "    ✓ ", style="yellow" if failed else "dim")
    if who:
        t.append(who_txt, style="dim cyan")
    t.append(shown, style="yellow" if failed else "dim")
    if ms is not None:
        t.append(tail, style="dim italic")
    console.print(t)


def error(text):
    """出错。亮红 —— 这是对话级,用户必须看见"""
    console.print(Text("  ✗ " + str(text), style="bold red"))


def warn(text):
    console.print(Text("  ⚠ " + str(text), style="yellow"))


def status(text, fold=None):
    """状态级:团队上下线、后台任务、压缩。dim —— 存在的意义是"看得见有这事",
    不是"要你读"。全亮了会把真正的对话淹没

    fold=(kind, title, body):顺带把全文登记进折叠处,供 /展开 调出来。
    做成参数而不是"再调一次 ui.fold()",是为了让**屏幕上那句摘要**和**折起来的全文**
    出自同一次调用 —— 分两处写迟早会飘,那时展开出来的东西对不上屏幕上的标题
    """
    if fold is not None:
        _fold(fold[0], "", fold[1], fold[2])
    console.print(Text("  · " + str(text), style="dim"))


def debug(text):
    """调试级:[bus] [scheduler] [mcp] 这些。默认整段不显示"""
    if UI_VERBOSE:
        console.print(Text("      " + str(text), style="grey35"))


def banner(lines):
    """开局那张横幅。每条 (文字, 样式),自己排"""
    console.print()
    t = Text()
    for s, style in lines:
        t.append(s, style=style)
    console.print(t)
    rule()
    console.print()


# ---------------------------------------------------------------------------
# 反问:从读线程手里把终端借过来问一句话
# ---------------------------------------------------------------------------
#读线程进入 prompt_toolkit 模式后置位。ask_sync 靠它区分**两种"拿不到 app"**,
#这两种的处理方式完全相反,分不清就会出 V6 那个 bug(见下面 wait_for_app)
PT_ACTIVE = threading.Event()

#UI_VERBOSE=1 时 ask_sync 全程写文件。**必须写文件不能 print**:
#patch_stdout 换掉了全局 sys.stdout,print 出去会被代理重排,时序读不准
_log_lock = threading.Lock()
_log_fh = None


def _log(*args):
    global _log_fh
    if not UI_VERBOSE:
        return
    with _log_lock:
        if _log_fh is None:
            _log_fh = open("ui_ask.log", "w", encoding="utf-8")
        ts = time.strftime("%H:%M:%S")
        ms = int((time.time() % 1) * 1000)
        _log_fh.write(f"[{ts}.{ms:03d}] " + " ".join(str(a) for a in args) + "\n")
        _log_fh.flush()


def read_input_plain(inbox):
    """没有终端时的读线程主体。管道驱动/重定向走这条 —— 和改造前完全一样"""
    while True:
        try:
            inbox.put(input("User: "))
        except (EOFError, KeyboardInterrupt):
            inbox.put(None)     #None 当作退出信号
            return


def read_input(inbox):
    """读线程的主体 —— 专职读输入(它阻塞没关系,主循环不等它),把行塞进队列。

    **为什么放在 ui.py 而不是 main.py**:它和 ask_sync 是一对 ——
    一个把终端的所有权拿走,另一个要临时借回来。共用 PT_ACTIVE,改一个必须看另一个。
    放一起,顺带让探针能 import 到真东西(能测的代码才敢改)。

    **为什么不是裸 input()**:主循环那条线程要往屏幕上打东西(工具调用、状态、
    子agent的横幅),裸 input() 下两边互相踩 —— 模型正打到一半,提示符还杵在下面,
    输出从它身上碾过去,屏幕就花了。patch_stdout() 把 sys.stdout 换成代理:
    有人打印时先擦掉提示符,打完再画回来。
    """
    from prompt_toolkit import PromptSession
    from prompt_toolkit.formatted_text import ANSI
    from prompt_toolkit.patch_stdout import patch_stdout

    if not sys.stdin.isatty():
        #管道驱动/重定向:没有终端可接管,patch_stdout 连 output 都建不出来
        #(会抛 NoConsoleScreenBufferError)。退回裸 input(),和以前一样。
        #注意这条路上 PT_ACTIVE 永远不置位 —— ask_sync 会据此判"真没在用",
        #立刻降级,而不是白等 2 秒
        debug("读线程: stdin 不是 tty,退回裸 input()")
        return read_input_plain(inbox)

    session = PromptSession()
    PT_ACTIVE.set()     #置位之后 ask_sync 才敢走 run_in_terminal
    #★ raw=True 不能少。默认的 raw=False 会让代理走 Vt100_Output.write(),
    #那里有一句 `data.replace("\x1b", "?")` —— 它把每个 ESC 换成问号,
    #于是屏幕上就是 `?[2;3m  › 思考 ?[0m`(详见文件顶部那段)。
    #raw=True 走 write_raw(),原样放行。rich 本来就是**有意**发转义码的,
    #patch_stdout 那句"安全写法"针对的是别人的 print,不是它
    with patch_stdout(raw=True):
        while True:
            try:
                #handle_sigint=False:别让 prompt_toolkit 装自己的 SIGINT 处理器。
                #装了 Ctrl+C 就只作用于这个提示符;不装则走 Python 默认处理 ——
                #KeyboardInterrupt 抛在**主线程**,由 user_loop 那个 except 接住。
                #这正是主循环"唤醒源永远在主线程"那条不变量要的
                line = session.prompt(ANSI(PROMPT_ANSI), handle_sigint=False)
                inbox.put(line)
            except (EOFError, KeyboardInterrupt):
                inbox.put(None)     #None 当作退出信号
                return


def wait_for_app(timeout=2.0, spin=0.002):
    """自旋等 app 回到运行态。

    ★ 这是 V6 那个"按什么都没反应"的修法,别删。

    读线程的两轮 prompt 之间有个**缝**:session.prompt() 返回 → inbox.put(line)
    → 转身再进 session.prompt()。而 set_app 退出时会 `session.app = previous_app`
    (也就是 None),所以这一瞬间 get_app_or_none() 是 None。

    主线程对 inbox 的反应是微秒级的(实测 0.01ms),所以**每一次**都精确掉进这个缝。
    实测缝宽 ~2.6ms。

    等不到(超过 2 秒)→ 返回 None,由调用方记**拒绝**,绝不能降级去抢控制台。
    """
    from prompt_toolkit.application.current import get_app_or_none

    t0 = time.perf_counter()
    n = 0
    while True:
        app = get_app_or_none()
        if app is not None and getattr(app, "_is_running", False) and app.loop is not None:
            _log(f"wait_for_app: 第 {n} 次拿到 app,{(time.perf_counter() - t0) * 1000:.1f}ms")
            return app
        n += 1
        if time.perf_counter() - t0 >= timeout:
            _log(f"wait_for_app: 超时 {timeout}s(试了 {n} 次)")
            return None
        time.sleep(spin)


def ask_sync(text, timeout=25):
    """问用户一句话,阻塞等回答。返回 (answer, why)。

    why 的含义(调用方**必须**按这个判,别只看 answer 是不是 None):
        'ok'        问到了,answer 是用户敲的那行
        'fallback'  读线程没在用 prompt_toolkit(管道驱动/非交互),走的老 input()
        'eof'       读不到输入(stdin 关了:管道跑、cron 唤醒时没有终端)
        'timeout'   投递进事件循环了,但 timeout 秒内用户没回答
        'no-app'    在跑 prompt_toolkit 却等不到 app —— 终端所有权不明,不敢抢

    后三个都是**拒绝**。权限询问问不到人,只能往严的方向倒。

    为什么不能简单降级成 input():读线程在用 prompt_toolkit 时,终端的所有权
    在它手上。主线程再开一个 input() 就是两边抢同一个控制台,结果就是按键被吃掉 ——
    表现成"提示出来了,但按什么都没反应"。所以只有**确定**它没在用(PT_ACTIVE
    没置位)才降级。
    """
    from prompt_toolkit.application.run_in_terminal import run_in_terminal

    if not PT_ACTIVE.is_set():
        _log("ask_sync: prompt_toolkit 未启用 → 裸 input()")
        try:
            return input(text), "fallback"
        except (EOFError, KeyboardInterrupt):
            return None, "eof"

    app = wait_for_app(2.0)
    if app is None:
        _log("ask_sync: 等不到 app,记为拒绝")
        return None, "no-app"

    box, ev = {}, threading.Event()

    def do_input():
        #**在事件循环的线程里跑**(run_in_terminal 会先 app.input.detach() +
        # cooked_mode(),跑完再装回去),所以这里的 input() 是真的在读控制台
        try:
            box["v"] = input(text)
        except BaseException as e:
            box["e"] = f"{type(e).__name__}: {e}"
            _log("do_input 抛异常:", box["e"])
        finally:
            ev.set()

    def schedule():
        try:
            run_in_terminal(do_input, in_executor=True)
        except BaseException as e:
            _log("schedule: run_in_terminal 抛异常:", type(e).__name__, e)
            ev.set()

    app.loop.call_soon_threadsafe(schedule)

    if not ev.wait(timeout):
        _log("ask_sync: Event.wait 超时")
        return None, "timeout"
    if "e" in box:
        return None, "eof"
    return box.get("v"), "ok"


def ask_yes_no(text, timeout=25):
    """ask_sync 的 y/n 版 —— 给权限询问用。返回 (approved, why)。

    why 在这里**收窄成四种**,调用方不用去理解 ask_sync 内部那套:
        'answered'  问到了人。approved 是"他敲的是不是 y"
        'eof'       读不到输入(stdin 关了:管道跑、非交互、cron 唤醒时没有终端)
        'timeout'   投递进去了,但 timeout 秒内没人回答
        'no-app'    在跑 prompt_toolkit 却等不到 app —— 终端所有权不明,不敢抢

    ★ 'fallback' 在这里被并进 'answered':它表示"走的是旧 input(),但**确实问到了**"。
      ask_sync 才需要区分这两者,调用方不需要 —— 上面 HOOKS 那边一开始就是漏了
      这一条,把用户明确敲的 n 报成了"读不到输入"(已修)

    **只有明确敲了 y 才算批准**。回车、yess、大写 Y 之外的任何东西一律当拒绝:
    这是唯一一个"默认值选错就出事"的地方,不能像别的提示那样把空回车当默认同意
    """
    answer, why = ask_sync(text, timeout)
    if why in ("eof", "timeout", "no-app"):
        return False, why
    # 'ok' 和 'fallback' 都落这 —— 两条路都真的问到了人
    return (answer or "").strip().lower() == "y", "answered"


if __name__ == "__main__":
    # 自检:python ui.py —— 对着这个提意见,比对着文字描述快
    banner([
        ("Claude Mini", "bold"),
        ("   输出层自检   UI_VERBOSE=0(调试级整段不显示)", "dim"),
    ])
    #用户那一行不是渲染出来的,是提示符本身(见 PROMPT_ANSI)。演示块里手写一份:
    console.print(Text.assemble(("▌ ", "bold magenta"), ("看看当前目录有什么", "bold")))
    thinking("先 ls,再按模块归类。注意用中文回答…", 14)
    tool_call("bash", "ls -1 | head -20")
    tool_result("27 个文件", ms=41)
    tool_call("bash", "ls -1 *.py | wc -l")
    tool_result("18 个 .py", ms=38)
    tool_call("write_file", "memory/x.md")
    tool_result("❌ 需要人工确认,但当前没有可用的交互终端(读不到用户输入),已按拒绝处理。")
    assistant("当前目录下有 **27 个文件**,其中 18 个是 Python 模块。\n\n- `main.py` —— 入口与输入循环\n- `claude.py` —— Agent 主循环")
    status("后台子agent bg_0002 已提交")
    warn("这个要小心")
    error("这一轮失败了:请求超时")
    debug("[bus] member_01 → main: 我这边查完了")
    console.print()
    console.print(Text("  ↓ 调试级,默认不显示。UI_VERBOSE=1 python ui.py 再看一遍", style="italic grey42"))

    #折叠/展开。上面那些 thinking / tool_result 已经各自登记了一条,这里把
    #"怎么调出来"演一遍 —— 序号是全局的,所以直接引用上面那几条
    console.print()
    rule()
    console.print(Text("  折叠 / 展开    (敲 /帮助 看全部命令)", style="bold"))
    rule()
    run_command("/折叠列表")
    console.print()
    console.print(Text("  · 上面每一条都能重打全文。比如 /展开 1 ——", style="dim"))
    run_command("/展开 1")
    console.print(Text("  · 展开会把它**重新打一遍**在当前位置,不是把上面那行改开:", style="dim"))
    console.print(Text("    终端的历史区改不了(鼠标点它只会选中文本),所以只能用重打这条路", style="dim"))
