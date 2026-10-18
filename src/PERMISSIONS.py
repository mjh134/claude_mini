import os
import re
import shlex
from enum import Enum

import paths

class Permission(Enum):
    ALLOW = "allowed"
    ASK = "ask"
    DENY = "deny"


 #需要二次询问的命令
ASK_COMMANDS = [
    "rm",
    "rmdir",
    "del",
    "format",
    "move",
    "mv",
    "cp",
    "rename",
    "ren"
]


#---------------------------------------------------------------------------
#DENY 这一档:先切命令结构,再按结构判 —— **不做整串子串搜索**
#
#原来 DENY_COMMANDS 是一张字符串表,靠 `if denied in command_lower` 匹配。那种写法
#两个方向都会错,而且错法不一样:
#
#  · 往窄了错(误伤),这是最刺眼的:`"rm -rf /"` 是 `"rm -rf /tmp/scan"` 的子串
#    → 在 /tmp 下清临时文件被硬拒。而下游 HOOKS.py 拿到的返回语是
#    "不要再尝试执行重复命令" —— 那句话是在叫模型**放弃**,不是叫它换个写法。
#  · 往宽了错(漏判):表里只写了 `> /dev/sda`,那 `> /dev/nvme0n1` 就整个漏掉。
#  · 还有一种谁都没想到的:**引号里的东西是死的**。`echo "> /dev/sda"` 只是在
#    打印一行字,shell 根本不执行那个重定向,可子串搜索照样判 DENY。
#
#所以改成:把命令行按 shell 分隔符切成"简单命令",每条找出真正的程序词和参数,
#再按"程序是什么 / 目标是哪里"判。判定**只可能更严**(见 test_permissions.py 的
#逐条比对:老代码拒掉的东西,新代码一条都不许放行,除了明确的误伤)。
#
#★ 唯一一处**故意不放宽**的策略在 dd 那条,下面有注释说明为什么。
#---------------------------------------------------------------------------

#把一条命令行切成简单命令用的分隔符。&& 和 || 要在单个字符前面判,否则会被切成两个空段。
#&(后台)和换行也算 —— `sleep 1 & rm -rf /` 的后半段一样危险
_SEP2 = ("||", "&&")
_SEP1 = (";", "|", "&", "\n")

#执行别的程序的"包装器":跳过它们才能看见真正要跑的那个程序。
#★ sudo 必须在列:否则 `sudo rm -rf /` 的程序词会变成 "sudo",
#  下面所有按程序名判的规则**同时失效** —— 这是权限检查里最经典的绕过。
_WRAPPERS = {
    "sudo", "doas", "env", "nohup", "time", "command",
    "exec", "nice", "ionice", "setsid", "stdbuf", "xargs",
    #busybox 的第一个参数是"要跑哪个 applet",所以它也是个包装器:
    #   busybox rm -rf /   → 跳过 busybox 之后才能看见 rm
    "busybox",
}
#★ su 不在这里,在下面的 _INTERPRETERS 里。它的 `-c` 后面跟的是**要执行的命令**,
#  当成普通"带值的开关"跳掉的话,su -c "rm -rf /" 就整个漏了

#会**执行**自己参数里那段代码的解析器。
#★ 这一组是"引号里的东西是死的"那条规则的另一半 —— 引号里的内容
#     对 echo 是死的:  echo "rm -rf /"      只是打印
#     对 sh -c 是活的:  sh -c "rm -rf /"    那个字符串会被当命令跑
#  所以**不能只看有没有引号**,得看谁在执行它。只按引号判会两边都错。
_INTERPRETERS = {
    "sh", "bash", "zsh", "dash", "ksh", "ash", "csh", "tcsh", "fish",
    "su", "eval",
    "cmd", "cmd.exe", "powershell", "powershell.exe", "pwsh",
}

#解析器用什么开关把"要执行的代码"引进来。
#★ 全小写存:cmd 的 /C 和 /c 是同一个开关,比之前统一 lower() 一下
_CODE_FLAGS = {"-c", "/c", "-command", "--command"}

#递归深度上限。`sh -c 'sh -c "sh -c ..."'` 这种套娃必须有底,
#否则一个构造出来的长命令能把权限检查拖死(它跑在工具调用的路径上,是同步的)
_MAX_NEST_DEPTH = 4

#包装器自己那些"后面还跟一个值"的开关。跳包装器时必须连这个值一起跳 ——
#   sudo -u root rm -rf /        ← 只跳 -u 的话,程序词会认成 "root",
#   nice -n 10   rm -rf /          rm 那几条规则就全失效了
#★ 只列真的带值的那些,不列 -b/-f/-i 这类布尔开关:把布尔的也当带值跳,
#  `sudo -b rm -rf /` 就会连 rm 一起跳掉,反而漏判。
#★ 不追求列全:漏掉一个的后果是程序词认偏。而认偏之后**不会**静默放行 ——
#  _unidentified 兜住这种"读不懂"的命令,让它落到 ASK(仍要问人)。
#★ su 不在此列(它整个搬去 _INTERPRETERS 了),所以这里只管剩下这几个包装器
_WRAPPER_VALUE_FLAGS = {
    "-u", "-g", "-p", "-U",      # sudo / doas / env:用户、组、提示符
    "-C", "-T", "-r", "-t",      # sudo:目录、超时、角色、类型
    "-n", "-c", "-s",            # nice / ionice / xargs:调整值、类、长度上限
    "-o", "-e", "-i",            # stdbuf / ionice / xargs:缓冲模式、类、替换串
    "-S", "-P",                  # env / sudo / xargs:分隔串、并行度
}

#关机/重启这一类。按程序名精确判,不用子串 —— `cat shutdown.log` 不该被拦
_POWER_COMMANDS = {"shutdown", "reboot", "halt", "poweroff"}

#裸块设备。★ 每一条都带数字或分隔符做边界,否则 `/dev/ram` 会把 `/dev/random` 也吃进来
_RAW_DEVICE_RE = re.compile(
    r"^/dev/(?:sd[a-z]|hd[a-z]|vd[a-z]|xvd[a-z]|nvme\d|mmcblk\d"
    r"|disk/|loop\d|ram\d|dm-\d|md\d|mapper/)",
    re.IGNORECASE,
)

#重定向到某个目标:> /x、>>/x、2>/x、&>/x 都算
_REDIRECT_RE = re.compile(r"(?:[0-9&]+)?>>?\s*([^\s;|&()<>]+)")


def _split_simple_commands(command: str):
    """按 shell 分隔符切成简单命令,**引号里的分隔符不算**。

    没直接用 shlex 切,是因为 shlex 会把引号吃掉、也会把 `>` 当成普通词,而后面
    认重定向还要拿原始形态。所以这里自己扫一遍字符。
    """
    parts, buf, quote, i = [], [], None, 0
    n = len(command)
    while i < n:
        ch = command[i]

        if quote:                                  #引号里:原样攒,只找配对的收尾引号
            if ch == quote:
                quote = None
            elif ch == "\\" and quote == '"' and i + 1 < n:
                buf.append(ch)
                i += 1
                ch = command[i]
            buf.append(ch)
            i += 1
            continue

        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
            i += 1
            continue

        if ch == "\\" and i + 1 < n:                #转义:把下一个字符一起吃掉
            buf.append(ch)
            i += 1
            buf.append(command[i])
            i += 1
            continue

        pair = command[i:i + 2]
        if pair in _SEP2:
            parts.append("".join(buf))
            buf = []
            i += 2
            continue

        if ch in _SEP1:
            parts.append("".join(buf))
            buf = []
            i += 1
            continue

        buf.append(ch)
        i += 1

    parts.append("".join(buf))
    return [p for p in (p.strip() for p in parts) if p]


def _tokens(part: str):
    """把一条简单命令切成词。

    shlex 切不动(引号不配对等)时退回按空白切 —— **绝不让异常穿出去**:
    这个函数是权限检查的一部分,它崩了就等于这次调用没有权限检查。
    退回的切法粗糙一些,但那只会让判定偏保守(该切的没切开 → 少认出一个程序词),
    不会把危险命令放行。
    """
    try:
        return shlex.split(part, posix=True)
    except ValueError:
        return part.split()


def _program(tokens):
    """找出这条简单命令真正要执行的程序(取 basename、转小写),以及它的参数。

    跳过三类前缀:VAR=value 环境赋值、_WRAPPERS 里的包装器、以及包装器自己的参数。
    ★ 包装器的参数要连着跳:`nice -n 10 rm -rf /` 里 `-n` 后面那个 `10` 也是 nice 的,
      不跳的话程序词会认成 "10",rm 那几条规则又全失效了。
    """
    i = 0
    while i < len(tokens):
        t = tokens[i]

        head, eq, _ = t.partition("=")
        if eq and head and not t.startswith("-") and head.replace("_", "").isalnum():
            i += 1                                  #VAR=value
            continue

        base = os.path.basename(t).lower()
        if base in _WRAPPERS:
            i += 1
            while i < len(tokens):
                w = tokens[i]
                if w.startswith("-"):
                    takes_value = w in _WRAPPER_VALUE_FLAGS
                    i += 1
                    if takes_value and i < len(tokens):
                        i += 1                      #连这个开关的值一起跳
                    continue
                if w.isdigit():
                    i += 1                          #nice -n 10 里那个孤零零的 10
                    continue
                break
            continue

        return base, tokens[i + 1:]

    return "", []


def _blank_quoted(part: str):
    r"""把引号里的内容和转义字符挖掉,换成等长空格。

    ★ 这一步专治"引号里的东西是死的":`echo "> /dev/sda"` 那个 > 只是要打印的文本,
      shell 不会执行它。拿整串做子串搜索分不出这层,挖掉之后就分得出了。
      转义同理:`\>` 是字面的 >,不是重定向。
    """
    out, quote, i, n = [], None, 0, len(part)
    while i < n:
        ch = part[i]
        if quote:
            if ch == quote:
                quote = None
            out.append(" ")
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            out.append(" ")
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            out.append("  ")                        #转义符和被转义的字符都是死的
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _write_targets(part: str, tokens):
    """这条命令往哪些东西写:重定向目标(> /x、2>/x)+ dd 的 of= """
    targets = []
    for m in _REDIRECT_RE.finditer(_blank_quoted(part)):
        targets.append(m.group(1))
    for t in tokens:
        if t.startswith("of="):
            targets.append(t[3:])
    return targets


def _resolves_to_root(arg: str):
    """这个参数是不是文件系统根。

    要盖住的写法:  /   //   /*   /.*   /./   /../   /*/..   /***   /??
    要放过的写法:  /tmp   /var/log   /root   /etc/*   /dev/sda   ~   $HOME

    ★ 做法:砍掉开头的斜杠之后,剩下的字符必须**全部**来自 `/ . * ? [ ]` 这一小撮。
      这样"通配符 + 点 + 斜杠"的任意组合都自动归到根(`/./`、`/../`、`/*/..`
      都是同一条规则盖住的,不用为每种写法单列一条),而只要出现一个真正的名字
      字符(`/tmp` 里的 t、`/etc/*` 里的 e)就不算根。
    ★ 不做完整路径解析 —— 这里判的是"这条命令是不是冲着根去的",不是"这个路径
      最终指向哪"。所以 `/tmp/../..` 看不出来;那类绕法是 ASK 那一档的事,
      不是 DENY 这一档要解决的。
    """
    a = arg.strip()
    if not a.startswith("/"):
        return False
    rest = a.lstrip("/")
    return rest == "" or all(ch in "*?[]/." for ch in rest)


def _nested_code(prog: str, args):
    """如果这条命令是在**执行**一段代码,把那段代码取出来;否则 None。

    sh -c "rm -rf /"        → "rm -rf /"
    su -c "rm -rf /" root   → "rm -rf / root"
    eval "rm -rf /"         → "rm -rf /"
    echo "rm -rf /"         → None      ← echo 只是打印,引号里的是死的

    ★ 最后那两行的对比就是这个函数存在的全部理由。少了它,结构化判法会漏掉
      `sh -c "rm -rf /"` —— 而老代码靠子串**恰好**是拦得住的。修 bug 修出一个
      新绕过通道,比原来的误伤严重得多。
    """
    if prog == "eval":
        return " ".join(args) if args else None

    if prog not in _INTERPRETERS:
        return None

    for i, a in enumerate(args):
        if a.lower() in _CODE_FLAGS and i + 1 < len(args):
            #-c 后面可能是一整段脚本(还带着自己的参数),拼回去一起递归
            return " ".join(args[i + 1:])
    return None


def _is_assignment(token: str):
    """这一词是不是 VAR=value 形式的环境赋值(和 _program 里那段同源)"""
    head, eq, _ = token.partition("=")
    return bool(eq) and bool(head) and not token.startswith("-") \
        and head.replace("_", "").isalnum()


def _unidentified(command: str, _depth: int = 0):
    """这条命令里有没有"读不懂"的简单命令 —— 有就返回那条的样子,没有返回 None。

    "读不懂"= 有词,但认不出程序词(全被当成包装器/开关/赋值吃掉了)。会出现这种的是:
        sudo "rm -rf /"   ← 整段被引号包成一个词(基本无害:sudo 会去找一个名字
                             就叫 "rm -rf /" 的程序,找不到,报错)
        sudo -u root      ← 包装器后面没跟命令
        X=1               ← 光赋值,没有命令

    ★ 这类基本都无害。列出来不是为了拦它们,而是因为**"读不懂"本身是个信号**:
      解析器没吃透这条命令,那么"没有任何 DENY 规则命中"就不能当作"安全"的证据。
      权限检查读不懂一条命令时该往严的方向倒 —— 和 _tokens 里那条是同一条原则。
      (纯赋值的那一种不算读不懂:`X=1` 本身就是一条完整、无副作用的命令)

    ★ 还有一类更该算进来:**shlex 根本切不动、走了 _tokens 那条降级路径的命令**。
      降级是按空白硬切的,切出来的词会带着引号:
          sh -c 'sh -c "rm -rf /"      ← 单引号没配对
      硬切得到 ['sh','-c',"'sh",'-c','"rm','-rf','/"'],于是程序词算出来是 `'sh`
      (带个前引号),既不是 sh 也不是 rm —— 递归在这里就断了,rm 永远不会被看见。
      真实 shell 遇到不配对的引号会直接报语法错、什么都不执行,所以这类命令本身是死的;
      但"我们的解析器在这里失灵了"是真的,所以按读不懂处理。
    """
    if _depth >= _MAX_NEST_DEPTH:
        return None
    for part in _split_simple_commands(command):
        try:
            shlex.split(part, posix=True)
        except ValueError:
            return part                            #降级路径:解析失灵,别放行
        tokens = _tokens(part)
        if not tokens:
            continue
        prog, args = _program(tokens)
        if prog == "" and not all(_is_assignment(t) for t in tokens):
            return part
        nested = _nested_code(prog, args)
        if nested:
            hit = _unidentified(nested, _depth + 1)
            if hit:
                return hit
    return None


def _command_words(command: str, _depth: int = 0):
    """这条命令行里出现的所有"命令词",**包括嵌套在解析器参数里的那些**。只给 ASK 那一档用。

    两个都不行的写法,合起来才行:
      · 只按空白切原串:`sh -c "rm -rf /tmp/scan"` 切出 `'"rm'`,带个引号 ≠ `rm` → 漏。
      · 只取程序词:`docker run alpine rm -rf /data` 的程序词是 docker,
        `find . -exec rm {} ;` 是 find —— 而这俩恰恰是 ASK 该拦的,只取程序词全放过去。

    所以是**两边都算**:原串按空白切的词 ∪ 递归进解析器参数再切的词。
    """
    words = set(command.lower().split())
    if _depth >= _MAX_NEST_DEPTH:
        return words
    for part in _split_simple_commands(command):
        tokens = _tokens(part)
        if not tokens:
            continue
        prog, args = _program(tokens)
        nested = _nested_code(prog, args)
        if nested:
            words |= _command_words(nested, _depth + 1)
    return words


def _deny_reason(command: str, _depth: int = 0):
    """该不该 DENY。返回理由字符串(给人看),不该拒就返回 None。

    _depth 是递归层数,只在解析器嵌套时增长(sh -c 'sh -c ...'),由
    _MAX_NEST_DEPTH 封顶 —— 这个函数跑在工具调用的同步路径上,不能被构造出来的
    深套娃拖住。
    """
    for part in _split_simple_commands(command):
        tokens = _tokens(part)
        if not tokens:
            continue
        prog, args = _program(tokens)

        #--- 程序级 ---
        #★ mkfs 用 startswith 而不是相等:真实形态是 mkfs.ext4 / mkfs.xfs / mkfs.vfat,
        #  相等匹配一个都盖不住(老写法是子串 `"mkfs" in command`,恰好能盖住,
        #  所以这里必须用 startswith 才能不丢)
        if prog.startswith("mkfs") or prog == "mke2fs":
            return f"格式化文件系统:{prog}"

        if prog in _POWER_COMMANDS:
            return f"关机/重启命令:{prog}"

        #★ 这一条**保持原策略不变**:老代码是 `"dd if=" in command`,即 dd 只要带
        #  if= 就拒。我没有借修 bug 的机会把它放宽成"只有目标是块设备才拒" ——
        #  放宽一条安全规则是产品决策,不该混在修 bug 里顺手做掉。这次只修"子串"那一层:
        #  要求 dd 真的是程序词(所以 `echo "dd if=x"` 不再误伤),参数真的以 if= 开头。
        #  想放宽的话,把它改成只拦 `of=` 指向块设备的那种,那是有意为之的一次改动。
        if prog == "dd" and any(a.startswith("if=") for a in args):
            return "dd 从设备/文件读原始数据(if=)"

        #--- 目标级:往裸设备上写 ---
        #这是老代码 `> /dev/sda` 那条规则的本意。改成按"目标是块设备"判之后,
        #顺带把 /dev/sdb、/dev/nvme0n1、/dev/mapper/* 一起盖住了(老写法只认得 sda)。
        #★ 判的是**写目标**,不是"命令里出现了块设备" —— 否则 `fdisk -l /dev/sda`
        #  这个纯读取的操作会被误伤,而那在渗透测试里是要用的
        for target in _write_targets(part, tokens):
            if _RAW_DEVICE_RE.match(target.strip()):
                return f"写入目标是裸设备:{target.strip()}"

        #--- rm 到根 ---
        #老代码写死 `"rm -rf /"`。这里只要求"程序是 rm,而且某个参数就是根",
        #不看 -r/-f:`rm -rf /`、`rm -fr /`、`rm -rf /*`、`rm -rf / --no-preserve-root`
        #都该拦,而加不加 flag 不该影响这个判断
        if prog == "rm" and any(_resolves_to_root(a) for a in args):
            return "rm 的目标是文件系统根"

        #--- 嵌套:解析器和包装器不一样,它**会把参数当命令跑**,所以要递归下去看 ---
        #★ 这一段是被测试逼出来的。改完之后我拿"老代码拒掉的东西"过了一遍没发现问题,
        #  但另一头有个洞:老代码的 `"rm -rf /" in command` 顺手盖住了
        #  `sh -c "rm -rf /"`,而结构化判法只看程序词 sh,会判 ALLOW ——
        #  修 bug 修出一条新的绕过通道。所以这两类命令必须递归。
        nested = _nested_code(prog, args)
        if nested and _depth < _MAX_NEST_DEPTH:
            reason = _deny_reason(nested, _depth + 1)
            if reason is not None:
                return f"{prog} 要执行的代码里有危险命令 —— {reason}"

    return None


#检查bash命令
def check_bash_permission(command: str):
    reason = _deny_reason(command)
    if reason is not None:
        return Permission.DENY

    # ASK 按"命令词"精确匹配,不做子串:
    #   "echo renaming log"  → ALLOW  (renaming 不是 ren)
    #   "ren old.txt new.txt" → ASK
    #★ 但这里要改掉一句曾经写在这儿的错话。原注释声称 "echo format complete" → ALLOW,
    #  **不成立**:按词匹配时 format 是这一行里一个独立的词,照样命中 → ASK。
    #  写测试时才发现的(test_permissions.py 的 ALLOW 用例把它抓了出来)。
    #  想让它 ALLOW 得改成"只看程序词",而那是错的 —— 见下面那段为什么不动 ASK。
    #
    #★ ASK 这一档**故意不跟着改成"只看程序词"**。按任意位置的词匹配确实会误伤
    #  (`echo cp` 也会 ASK),但它同时盖住了程序词看不出来的那些写法:
    #      docker run alpine rm -rf /data     ← 程序词是 docker
    #      find . -exec rm {} \;
    #  这几个才是真该拦的。ASK 是软闸(交互模式下用户点个头就过),宁可多问一句,
    #  不该为了少问一句把上面那些放过去。
    #
    #★ 但"按词匹配"的取材范围**必须扩到嵌套里去**,否则 `sh -c "rm -rf /tmp/scan"`
    #  就成了"套一层 shell 即可跳过确认"的通道 —— 原串按空白切出来的词是
    #  `'"rm'`,跟前缀引号粘在一起,比不中 rm。_command_words 把两边的词都算上。
    tokens = _command_words(command)
    for dangerous in ASK_COMMANDS:
        if dangerous in tokens:
            return Permission.ASK

    #★ 最后一道:读不懂就问,别放行。
    #  上面那句"不追求列全:漏掉一个的后果是程序词认偏 → 退回 ASK"原来**是句空话** ——
    #  程序词认偏之后代码并不会退回 ASK,它一路走到 ALLOW。这里把它补成真的。
    #  代价是 `sudo -u root` 这种残句会多问一句(它本来也没意义),
    #  换来的是"解析器看不懂"不再等于"放行"这个类别。
    if _unidentified(command) is not None:
        return Permission.ASK

    return Permission.ALLOW


def _memory_root():
    return paths.PROJECT_ROOT / os.getenv("MEMORY_DIR", "memory")


def touches_memory(path):
    """这个写入目标是不是落在长期记忆库里。

    解析方式必须和 run_write 真正落盘的方式一致:run_write 用的是 Path(path),
    相对路径 ⇒ 相对于**进程 cwd**。所以这里也走 abspath,而不是拿 _memory_root() 去拼字符串 ——
    这样 "memory/x.md" 和 "D:/…/memory/x.md" 两种写法会归到同一个绝对路径,
    "./a/../memory/x.md" 这种绕法也会在 abspath 里被规范化掉。
    normcase:Windows 路径不分大小写,"MEMORY/" 和 "memory/" 是同一个目录。
    比前缀时带上分隔符,免得 "…/memory_backup/" 被误判成 "…/memory/" 的子目录。
    解析失败按"碰了"处理(交给调用方 ASK):权限检查自己出错时该往严的方向倒,不能放行。
    """
    try:
        target = os.path.normcase(os.path.abspath(path))
        root = os.path.normcase(os.path.abspath(_memory_root()))
    except Exception:
        return True
    return target == root or target.startswith(root + os.sep)

#统一检查
def check_permission(tool_name: str, arguments: dict):
    """工具调用级判定:这一次调用准不准。返回 ALLOW / ASK / DENY。

    规则放哪一层,按这个分:
      - 硬性不变量(如 .env 永不覆盖)写在工具自己的 handler 里 —— 谁都绕不过;
      - 需要人来判断的("这看着不对,你确定吗")写在这里,交给 permission_hook:
        主线程会弹窗问用户,成员线程弹不了窗就直接拦下(见 HOOKS.py 的 permission_hook)。
    """

    if tool_name == "bash":
        command = arguments.get("command", "")
        return check_bash_permission(command)

    #write_file 是成员绕过 write_memory 的现成通道:成员手里就有 write_file,
    #直接往 memory/ 写文件就能改长期记忆,一个技巧都不用。
    #这里管的是"路径落在记忆库里",不是"这个工具危险" —— 写源码、报告、临时文件一概 ALLOW,
    #不打扰。合法改记忆的路径是 write_memory(它自己调 memory.py,不走 write_file 工具),
    #所以这条规则不会碍着正事。
    if tool_name == "write_file" and touches_memory(arguments.get("path", "")):
        return Permission.ASK

    return Permission.ALLOW
