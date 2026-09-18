"""终端控制台:把"用户输入的那半行字"从终端手里接管过来。

背景 —— 为什么需要这个模块:

    input(prompt) 写提示符时不带换行,写完就阻塞。光标于是停在 "User: " 后面,
    谁下一个往 stdout 写,就接在那一行上。那一行走完,提示符就被淹没,而且
    **没有任何东西会把它重画出来**(input 只在进入的那一刻写一次)。表现出来就是
    "User: 经常不显示,要等用户回车才出现" —— 回车后看到的那个提示符其实是
    read_input 循环里**下一个** input() 打的。

    改造前主线程自己卡在 input() 里,没有第二个写入者,所以是自洽的。
    把 input() 挪到读线程之后,多出了写入者,这个冲突就必然发生。

做法:

    输入侧用 msvcrt.getwch() 逐键读,自己回显、自己维护 buf。这样"用户输入了什么"
    就是我们手里的一个字符串变量,任何时候都能连着提示符一起擦掉重画。
    输出侧换掉 sys.stdout,让项目里所有 print 自动走这条路 —— 不用去改几十处调用点。
    子进程的输出被 bash_exec 用 capture_output 捕获、从不直接落终端,所以不存在
    绕过我们直接写终端的漏网者(见 bash_exec.py:44-60)。

    这两条都成立的前提是**先把控制台的行缓冲和回显清掉**(见 _enter_raw_mode)。
    不清的话实测会"吞字"、并且回车后多出一行提示符 —— 那两个症状是同一个根因,
    不是两个 bug。

让位协议:

    读线程是"默认持有者",它一直把提示符挂在行上。HOOKS 的权限确认也需要读一行,
    这时读线程必须把控制台交出来 —— 但它不能把用户打了一半的字弄丢。
    所以 _state[线程id]["buf"] 是**按线程存**的,让位只是交还所有权、不清 buf;
    下次进来 setdefault 取回同一个 dict,提示符和那半行字原样重现。

    交还之后能不能再拿,由一条 FIFO 队列说了算(见 _waiters):让位的人排到队尾,
    被让的人排在前头。这条队列是必需的 —— 让位者会立刻重试,不排队的话它会在
    被让者还没醒过来时把控制台又抢回去,把人饿死。

已知限制:

    输入行长到折行时,"\r + 空格 + \r" 只清得掉最后一行,上一行会留残影。
    要根治得用 ANSI 的 ESC[2K(Windows 上还需 ctypes 打开 VT 处理),这里不引。
    提示符 6 列、输入是短句时不会触发。

非交互场景:

    stdin 或 stdout 不是终端时不接管,read_line 内部退回 input()。
    原因是 getwch() 读的是**控制台**、不读 stdin —— 一旦接管,用管道喂 main.py
    的脚本驱动就失效了(bash_exec.py:51-53 的注释说明以前确实这么用过)。
"""
import atexit
import sys
import time
import threading
import unicodedata

try:
    import msvcrt
except ImportError:  # 非 Windows:接管模式不可用,read_line 全程退回 input()
    msvcrt = None

# 控制台输入模式的位。下面两个必须清掉,这是 getwch 方案能成立的前提 ——
# 不清的话实测会"吞字"和"多出一行提示符",原因见 _enter_raw_mode
ENABLE_PROCESSED_INPUT = 0x0001     # 留着:Ctrl+C 仍是信号,能打断正在跑的 agent
ENABLE_LINE_INPUT = 0x0002          # 清掉:否则 ReadConsole 一次吃掉一整行 → 吞字
ENABLE_ECHO_INPUT = 0x0004          # 清掉:否则控制台自己回显一遍,和我们重画打架
_RAW_MODE_CLEAR = ENABLE_LINE_INPUT | ENABLE_ECHO_INPUT

try:
    import ctypes
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
except Exception:                   # 非 Windows / 环境异常
    _kernel32 = None


def _raw_mode(mode):
    """把控制台输入模式改成本模块需要的样子。

    单独拆成纯函数是为了能离线测 —— 真的 SetConsoleMode 调用得有控制台才验得了。
    """
    return mode & ~_RAW_MODE_CLEAR

# kbhit 的轮询间隔。它同时是"让位"的响应延迟上限 —— 20ms 人感觉不出来,
# 而每秒 50 次的 kbhit 调用开销可以忽略
_POLL_SECONDS = 0.02

# read_line 让位时的返回值。读线程见到它就稍后重来(见 main.py 的 read_input)
YIELD = object()


def _width(text):
    """显示宽度:东亚宽/全角字符占两列,组合字符不占列,其余一列。

    擦行要按这个算空格数 —— 用 len() 算的话,中文输入会擦不干净。
    """
    n = 0
    for ch in text:
        if unicodedata.combining(ch):
            continue
        n += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return n


def _is_tty(stream):
    try:
        return bool(stream is not None and stream.isatty())
    except Exception:
        return False


class _Console:

    def __init__(self):
        # 一把锁护住下面所有字段,以及所有对终端的写入。
        # 需要它是因为写入者来自多条线程(主循环、成员线程、Scheduler、AgentRunner),
        # 而擦行-写-重画这三步必须原子,否则又会互相插进来。
        self._mu = threading.RLock()
        self._out = None            # 真正的 stdout,永远不经过包装器
        self._active = False        # install() 时按 isatty 决定
        self._owner = None          # 当前持有控制台的线程 ident
        self._state = {}            # tid -> {"prompt": str, "buf": str}
        # 在等控制台的线程 ident,**按先来后到排队**。必须是队列不能是集合:
        # 让位的读线程重试时会排到队尾,这才保证被它让的那个线程能真的上位。
        # 用集合的话两边会互相"谦让" —— 都觉得自己前面有人在等,于是谁都不拿,
        # 双双卡死(这个坑是实测出来的)。
        self._waiters = []
        self._erased = True         # 当前行上是否没有我们画的东西
        self._drawn = 0             # 上一次画在行上的显示宽度
        self._closing = False       # shutdown() 之后为真,让读线程收工
        self._hcon = None           # 控制台输入句柄,改过模式才非 None
        self._saved_mode = None     # 原来的模式,退出时要还回去

    # ---------- 安装 ----------

    def install(self):
        """能接管就换掉 sys.stdout,返回是否接管了。"""
        if msvcrt is None:
            return False
        # stdin 和 stdout 都得是真终端,理由见模块开头"非交互场景"
        if not (_is_tty(sys.stdin) and _is_tty(sys.stdout)):
            return False
        if isinstance(sys.stdout, _Stream):     # 重复安装
            return True
        self._out = sys.stdout
        self._active = True
        self._enter_raw_mode()                  # 清掉行缓冲和回显,失败也继续
        sys.stdout = _Stream(self, self._out)
        return True

    # ---------- 控制台模式 ----------

    def _enter_raw_mode(self):
        """清掉控制台的**行缓冲**和**回显**。这一步不做,整个方案是坏的。

        实测症状(两个都出自这里):
          * 行缓冲(ENABLE_LINE_INPUT):ReadConsole 是以"一整行"为单位工作的,
            我用 getwch 要一个字符,它可能把已经攒下的一整行都吃进去、只还我一个,
            **剩下的丢掉** → 打字吞字;
          * 回显(ENABLE_ECHO_INPUT):控制台自己把每个键打一遍,我也在打。
            它先回显、光标往下走,我的 "\\r" 擦行就落在**新的一行**上,
            于是又多画出一个 "User: " → 回车后多一行提示符。

        ENABLE_PROCESSED_INPUT 故意留着:留着 Ctrl+C 才是信号,才能打断正在跑的
        agent。清了它 Ctrl+C 会变成普通字符,只能被正在读的那个读者看到 ——
        agent 干活时按 Ctrl+C 就没反应了。

        拿不到控制台(不是终端/被重定向)就静默跳过,调用方照常往下走。
        """
        if msvcrt is None or _kernel32 is None:
            return
        try:
            h = msvcrt.get_osfhandle(sys.stdin.fileno())
            mode = ctypes.c_uint32()
            if not _kernel32.GetConsoleMode(ctypes.c_void_p(h), ctypes.byref(mode)):
                return
            self._hcon = h
            self._saved_mode = mode.value
            _kernel32.SetConsoleMode(ctypes.c_void_p(h),
                                     ctypes.c_uint32(_raw_mode(mode.value)))
        except Exception:
            self._hcon = None
            self._saved_mode = None

    def _restore_mode(self):
        """把控制台模式还回去。不还的话退出后 shell 不会回显你打的字。"""
        if self._hcon is None or self._saved_mode is None or _kernel32 is None:
            return
        try:
            _kernel32.SetConsoleMode(ctypes.c_void_p(self._hcon),
                                     ctypes.c_uint32(self._saved_mode))
        except Exception:
            pass
        self._hcon = None
        self._saved_mode = None

    # ---------- 底层写 ----------

    def _raw(self, text):
        """直接写真正的 stdout。调用方必须已持锁。"""
        self._out.write(text)
        self._out.flush()

    def _erase_seq(self):
        """擦掉当前行上我们画的东西,返回要写的字符串。调用方必须已持锁。"""
        if self._erased:
            return ""
        self._erased = True
        n, self._drawn = self._drawn, 0
        return "\r" + " " * n + "\r"

    def _draw_seq(self):
        """把当前持有者的提示符 + 已输入的字画出来。调用方必须已持锁。"""
        st = self._state.get(self._owner)
        if st is None:
            return ""
        line = st["prompt"] + st["buf"]
        self._erased = False
        self._drawn = _width(line)
        return line

    def _redraw(self):
        self._raw(self._erase_seq() + self._draw_seq())

    # ---------- 输出 ----------

    def write(self, text):
        """所有输出的唯一出口(由 _Stream 调用)。

        有人正把提示符挂在行上时,先擦掉它,写完再把提示符连同已输入的字画回来。
        print("a") 会调两次 write("a" 和 "\n"),靠 _erased 标志保证只擦一次、只重画一次。
        """
        if not text:
            return
        with self._mu:
            if not self._active or self._owner is None:
                self._raw(text)     # 没人在读输入,原样输出
                return
            # 擦行必须画行之前求值:两个函数都改 _erased/_drawn,
            # 顺序反了会让 _erased 停在 True(提示符画着却说行是干净的),下次就不擦了
            erase = self._erase_seq()
            tail = self._draw_seq() if text.endswith("\n") else ""
            self._raw(erase + text + tail)

    def flush(self):
        with self._mu:
            try:
                self._out.flush()
            except Exception:
                pass

    def shutdown(self):
        """进程要退出了:请读线程收工,并把它画在行上的提示符擦干净。

        不做这件事的话,终端最后会停在**一个没有换行的 "User: "** 上,
        shell 的提示符会紧接在后面 —— 这是接管控制台带来的新尾巴
        (以前 input() 时代,退出前最后一句 print 以换行结尾,收得干净)。
        """
        with self._mu:
            self._closing = True
            if self._owner is not None:
                self._raw(self._erase_seq())
                self._owner = None
            self._restore_mode()

    # ---------- 输入 ----------

    def _dequeue(self, me):
        """把我从等待队列里摘掉(不在队列里就什么都不做)。调用方必须已持锁。"""
        if me in self._waiters:
            self._waiters.remove(me)

    def _can_take(self, me):
        """控制台空着,而且队首就是我(或没人排队)。调用方必须已持锁。

        必须先来后到,不能只判"空着就抢":读线程让位后会立刻重试,而它重试时
        被它让的那个线程还没醒过来,空档期里读线程一抢就把人饿死了。
        """
        return self._owner is None and (not self._waiters or self._waiters[0] == me)

    def _take(self, me):
        """取得所有权并把提示符画出来。调用方必须已持锁。"""
        self._owner = me
        self._erased = True
        self._redraw()

    def _enter(self, me, prompt):
        """取得控制台。有人在读就先排到队尾,等他交出控制台。

        排队而不是抢:让位过的读线程排到队尾,被它让的那个(权限确认)排在前头,
        于是能真的上位。不做这个排队就会两边互等 —— 详见 _waiters 的注释。
        """
        with self._mu:
            st = self._state.setdefault(me, {"prompt": prompt, "buf": ""})
            st["prompt"] = prompt       # buf 保留:让位过再回来时那半行字还在
            if self._owner == me:       # 同线程重入,不再排队
                return
            if self._can_take(me):
                self._dequeue(me)
                self._take(me)
                return
            if me not in self._waiters:
                self._waiters.append(me)
        try:
            while True:
                with self._mu:
                    if self._can_take(me):
                        self._dequeue(me)
                        self._take(me)
                        return
                time.sleep(_POLL_SECONDS)
        except BaseException:
            with self._mu:
                self._dequeue(me)
            raise

    def read_line(self, prompt, yieldable=False):
        """读一整行。返回去掉换行的内容;让位时返回 YIELD。

        yieldable=True 的读者会在有别人等控制台时主动让位(读线程用);
        yieldable=False 的读者不让位(权限确认用 —— 它必须拿到答案才能继续,
        没有"稍后重来"这一说)。

        会抛 KeyboardInterrupt(Ctrl+C)和 EOFError(Ctrl+Z),与 input() 一致,
        这样 main.py 那圈 except 和退出路径一行都不用改。
        """
        if not self._active:
            return input(prompt)        # 非交互:原样,脚本驱动不受影响

        me = threading.get_ident()
        self._enter(me, prompt)
        parked = False
        try:
            while True:
                with self._mu:
                    # 有人在等控制台(权限确认),把位置让出来。
                    # 注意这里只清所有权,_state[me]["buf"] 留着 —— 下次进来原样恢复。
                    # 我持有控制台时不可能同时在等待队列里,所以 _waiters 非空
                    # 就等于"有人在等我让位"
                    if yieldable and self._waiters and self._owner == me:
                        self._raw(self._erase_seq())
                        self._owner = None
                        parked = True
                        return YIELD

                # 进程要退了,收工。只有 yieldable 的读者认这个标志 —— 那正是读线程;
                # 权限确认读到一半时进程还没走到退出,不能把它打断
                if self._closing and yieldable:
                    raise EOFError

                if not msvcrt.kbhit():
                    time.sleep(_POLL_SECONDS)
                    continue

                ch = msvcrt.getwch()

                # 方向键/功能键:getwch 先返回一个前缀,再返回一个扫描码。
                # 两个都得吃掉,否则扫描码会被当成普通字符塞进 buf(屏幕上就是乱码)
                if ch in ("\x00", "\xe0"):
                    msvcrt.getwch()
                    continue

                # 兜底:万一终端开了 VT 输入,方向键会长成 \x1b[A 这种。
                # 整串必须吃掉,否则后面的 '[' 'A' 会被当成正文塞进 buf。
                # (默认没开,正常走的是上面那条 \xe0 前缀的路)
                #
                # 两条约束在这里打架,必须分清楚:
                #   * 是序列 → 得整串吃掉,不然 '[' 'A' 落进 buf 变成乱码;
                #   * 不是序列 → ESC 后面那个字符是**正文**,一个都不能丢。
                # 原来写成 `if kbhit() and getwch() in ("[", "O")` 就踩了第二条:
                # kbhit() 为真时无条件 getwch(),拿到的若不是 '[' 或 'O',
                # 那个字符被这一句直接丢弃。ESC 后面跟回车 = 回车没了,
                # 用户看到的就是"按回车没反应"。输入法取消组字时会发 ESC。
                if ch == "\x1b":
                    if not msvcrt.kbhit():
                        continue                    # 光秃秃一个 ESC,丢掉无事
                    nxt = msvcrt.getwch()
                    if nxt in ("[", "O"):
                        while msvcrt.kbhit():
                            if not (" " <= msvcrt.getwch() <= "?"):
                                break               # 吃到了 0x40-0x7E 的终止字节
                        continue
                    if nxt in ("\x00", "\xe0"):
                        if msvcrt.kbhit():
                            msvcrt.getwch()         # ESC + 方向键:扫描码也吃掉
                        continue
                    ch = nxt                        # ← 回灌:下面照常当正文处理

                if ch in ("\r", "\n"):      # 回车:交卷
                    with self._mu:
                        st = self._state[me]
                        self._raw(self._erase_seq() + st["prompt"] + st["buf"] + "\n")
                        return st["buf"]

                if ch in ("\x08", "\x7f"):  # 退格
                    with self._mu:
                        st = self._state[me]
                        if st["buf"]:
                            st["buf"] = st["buf"][:-1]
                        self._redraw()
                    continue

                if ch == "\x03":            # Ctrl+C
                    raise KeyboardInterrupt
                if ch == "\x1a":            # Ctrl+Z
                    raise EOFError
                if ch < " ":                # 其余控制字符一律忽略
                    continue

                with self._mu:
                    self._state[me]["buf"] += ch
                    self._redraw()
        finally:
            with self._mu:
                self._dequeue(me)       # 防御:万一是在 _enter 里被异常打断的
                if not parked:
                    self._state.pop(me, None)
                if self._owner == me:
                    self._raw(self._erase_seq())
                    self._owner = None


class _Stream:
    """sys.stdout 的替身:每个写入都经过 _Console,好在提示符挂着时先擦行再重画。"""

    def __init__(self, console, target):
        self._console = console
        self._target = target

    def write(self, text):
        self._console.write(text)

    def flush(self):
        self._console.flush()

    def __getattr__(self, name):
        # 下划线开头的拒绝转发:否则 _target 自己还没赋值时会无限递归
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._target, name)   # encoding / errors / fileno / isatty ...


_console = _Console()

# 兜底:不管怎么退出的,都要把控制台模式还回去。
# 不还的话 shell 就不再回显你打的字了 —— 那比提示符画错还难受
atexit.register(_console._restore_mode)


def install():
    """接管控制台(交互式终端下)。返回是否接管了。"""
    return _console.install()


def read_line(prompt, yieldable=False):
    return _console.read_line(prompt, yieldable=yieldable)


def shutdown():
    """退出前调用,把提示符从行上擦掉(见 _Console.shutdown)。"""
    _console.shutdown()


def write(text):
    _console.write(text)
