#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""PERMISSIONS.check_bash_permission 的回归测试。

    python -m pytest test_permissions.py -q
    python test_permissions.py          # 不装 pytest 也能跑,自己打印结果

## 为什么这个文件值得存在

改的是**安全判定**。安全判定的返工方向是不对称的:

  · 改宽了会漏掉危险命令 —— 用户看不见,直到出事;
  · 改窄了只是多问一句 —— 吵,但安全。

所以这里最重要的一组用例不是"新代码该拒的拒了吗",而是
**test_no_old_deny_was_lost**:把老实现原样抄进来,拿一批命令同时喂给新老两份,
逐条要求"老代码拒掉的,新代码也必须拒",白名单里只有本次明确要修的误伤。
少一条就成了漏洞,而且这组用例会长久地替下一次改动守着这个边界。
"""

#直跑自举:把 src/ 加进 import 路径(pytest 模式走 tests/conftest.py,不靠这两行)
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", "src"))

import sys

from PERMISSIONS import Permission, check_bash_permission


#============================================================
# 老实现,原样抄自改造前的 PERMISSIONS.py(commit 前的 HEAD 版本)
#
# 抄一份而不是 import —— 改完之后那份就没了。它的唯一用途就是当对照物,
# 所以**不许**跟着新代码一起"优化",它必须永远是那个有 bug 的样子。
#============================================================

_OLD_DENY = ["rm -rf /", "mkfs", "dd if=", "> /dev/sda", "shutdown", "reboot"]
_OLD_ASK = ["rm", "rmdir", "del", "format", "move", "mv", "cp", "rename", "ren"]


def _old_check_bash_permission(command: str):
    command_lower = command.lower().strip()
    for denied in _OLD_DENY:
        if denied.lower() in command_lower:
            return Permission.DENY
    tokens = set(command_lower.split())
    for dangerous in _OLD_ASK:
        if dangerous in tokens:
            return Permission.ASK
    return Permission.ALLOW


#============================================================
# 用例表。每一条都是 (命令, 期望判定, 为什么)
#============================================================

DENY_CASES = [
    # --- rm 到根:老代码只认得字面 "rm -rf /",这些写法一个都不能漏 ---
    ("rm -rf /", "字面形态"),
    ("rm -fr /", "调换 flag 顺序"),
    ("rm -r -f /", "flag 分开写"),
    ("rm -rf /*", "通配符"),
    ("rm -rf /.*", "匹配根下的点文件,经典删库命令"),
    ("rm -rf /", "再确认一次"),
    ("rm -rf --no-preserve-root /", "GNU rm 的显式开关"),
    ("rm -rf //", "双斜杠"),
    ("rm -rf /./", "点当路径段"),
    ("rm -rf /../", "往上走还是根"),
    ("rm -rf /***", "多重通配符"),

    # --- 包装器不能成为绕过通道 ---
    ("sudo rm -rf /", "sudo 前缀"),
    ("sudo -u root rm -rf /", "sudo 带参数"),
    ("doas rm -rf /", "doas"),
    ("nice -n 10 rm -rf /", "nice 带值参数 —— 包装器参数没跳干净就会漏"),
    ("env X=1 rm -rf /", "env 带赋值"),
    ("nohup rm -rf / &", "nohup"),
    ("X=1 rm -rf /", "裸环境变量赋值前缀"),

    # --- 复合命令:危险的在第二段 ---
    ("cd /tmp && rm -rf /", "&& 后半段"),
    ("echo hi; rm -rf /", "; 后半段"),
    ("true || rm -rf /", "|| 后半段"),
    ("sleep 1 & rm -rf /", "& 后半段"),
    ("cd /tmp\nrm -rf /", "换行分隔"),

    # --- mkfs:真实形态带后缀 ---
    ("mkfs /dev/sda1", "裸 mkfs"),
    ("mkfs.ext4 /dev/sdb1", "mkfs.ext4 —— 相等匹配会漏,所以必须 startswith"),
    ("mkfs.xfs -f /dev/sdb", "mkfs.xfs 带 flag"),
    ("/sbin/mkfs.ext4 /dev/sdc1", "绝对路径,靠 basename 认出"),
    ("sudo mkfs.vfat /dev/sdd1", "包装器 + 后缀"),
    ("mke2fs /dev/sdb1", "mke2fs 是同族"),

    # --- 关机重启 ---
    ("shutdown -h now", "常规关机"),
    ("shutdown /s /t 0", "Windows 写法"),
    ("sudo shutdown -r now", "包装器"),
    ("/sbin/shutdown -h now", "绝对路径"),
    ("reboot", "裸 reboot"),
    ("sudo reboot -f", "强制重启"),

    # --- dd ---
    ("dd if=/dev/zero of=/dev/sda", "经典抹盘"),
    ("dd if=/dev/sda of=img.bin", "读盘 —— 原策略是带 if= 就拒,这里照旧"),
    ("dd if=in.bin of=out.bin", "普通文件对拷 —— 策略未放宽,仍然拒"),
    ("sudo dd if=/dev/zero of=/dev/nvme0n1", "包装器 + nvme"),
    #★ 这条我本来当成"该放行"写进了 ALLOW 用例,跑出来是 DENY —— 因为原策略就是
    #  "dd 带 if= 一律拒",而我没有借修 bug 的机会改它。测带宽的 dd 也被拒确实偏严,
    #  但那是有意保留的产品决策,不是子串 bug。想放宽的话见 PERMISSIONS.py 里那段注释。
    ("dd if=/dev/zero of=/dev/null bs=1M count=1", "测带宽 —— 偏严,但原策略如此,保留"),

    # --- 重定向到裸设备:老代码只写死了 "> /dev/sda" 一种,这里必须更全 ---
    ("cat img > /dev/sda", "老代码认得的那一种"),
    ("cat img >/dev/sda", "★ 不带空格 —— 老代码的子串 \"> /dev/sda\" 反而漏了它"),
    ("cat img 2>/dev/sda", "带 fd"),
    ("cat img >> /dev/sdb", "追加,另一块盘"),
    ("dd if=x of=/dev/sda1", "of= 形式"),
    ("echo x > /dev/nvme0n1", "nvme"),
    ("cat x > /dev/mapper/vg-root", "LVM"),
    ("cat x > /dev/mmcblk0", "SD 卡"),

    # --- 嵌套:解析器会**执行**参数里那段代码,所以必须递归下去看 ---
    #★ 这一组是写完之后才发现漏的。老代码的 `"rm -rf /" in command` 顺手盖住了
    #  `sh -c "..."` 这一整类,而"只看程序词"的结构化判法会判 ALLOW ——
    #  修子串 bug 差点修出一条新的绕过通道。这几条就是钉住它的。
    ('sh -c "rm -rf /"', "★ sh -c:引号里的是**活的**,和 echo 正相反"),
    ("sh -c 'rm -rf /'", "单引号版本"),
    ('bash -c "rm -rf /"', "bash"),
    ('su -c "rm -rf /" root', "su -c(所以 su 不能当普通包装器跳掉)"),
    ("busybox rm -rf /", "busybox 的 applet 形态"),
    ("busybox sh -c 'rm -rf /'", "busybox + sh -c 双层"),
    ('eval "rm -rf /"', "eval"),
    ('sh -c "cat x > /dev/sda"', "嵌套里的重定向也要认出来"),
    ('sh -c "dd if=/dev/zero of=/dev/sda"', "嵌套里的 dd"),
    ("sh -c 'cd /tmp && rm -rf /'", "嵌套里还有复合命令"),
    ("sudo sh -c 'rm -rf /'", "包装器 + 解析器"),
    ('sh -c "mkfs.ext4 /dev/sdb1"', "嵌套里的 mkfs"),
]

ASK_CASES = [
    # ★ 本次要修的核心:老代码在这里判 DENY
    ("rm -rf /tmp/scan", "★ 修的就是它:清 /tmp 临时文件,不该被判成删根"),
    ("rm -rf ./build", "相对路径"),
    ("rm -rf /var/log/old", "子目录"),
    ("rm -rf 1234/", "数字开头"),
    ("rm -f out.txt", "普通删文件"),
    ("rmdir emptydir", "rmdir"),
    ("cp a b", "复制"),
    ("mv x y", "移动"),
    ("del file.txt", "Windows del"),
    ("format d:", "格式化盘符"),
    ("ren old.txt new.txt", "重命名"),
    # ASK 是按"命令里出现这个词"判的,所以下面这些会误伤 —— 见文件末尾说明
    ("docker run alpine rm -rf /data", "★ 程序词是 docker,靠词匹配才拦得住"),
    ("find . -exec rm {} ;", "find -exec 里的 rm"),
    #下面三条是**保留下来的原有行为**,不是本次改动的目标。写测试时我一度以为
    #它们该 ALLOW,跑出来才想起 ASK 是按词匹配的:
    ("echo format complete", "★ format 是这一行里独立的词,按词匹配照样命中(原注释说它 ALLOW,是错的)"),
    ("rm", "无参数的 rm,什么也不碰 —— 但按词匹配就是会 ASK。无害,保留"),
    ("rm -rf", "同上,无目标"),

    # --- 嵌套也不能成为"套一层 shell 就跳过确认"的通道 ---
    #★ 这两条是老代码 DENY、修复中途会变成 ALLOW、最后落在 ASK 的。
    #  如果不把 ASK 的词表扩到嵌套里去,`sh -c "rm -rf /tmp/x"` 会直接放行 ——
    #  那等于告诉模型"给删除命令套一层 sh -c 就没人问了"。ASK 是软闸,但软闸也是闸。
    ('sh -c "rm -rf /tmp/scan"', "★ 套了 shell 的 rm —— 原串切词切出 '\"rm',比不中,必须递归取词"),
    ("sh -c 'rm -rf ./build'", "同上,相对路径版"),
    ('bash -c "cp a b"', "嵌套里的 cp"),
]

ALLOW_CASES = [
    # --- 老代码的误伤:引号里 / 只是文本,不是命令 ---
    ('echo "> /dev/sda"', "★ 引号里的重定向是死的,shell 不会执行"),
    ('echo "rm -rf /"', "★ 引号里的删根命令是死的"),
    ('echo "dd if=x"', "引号里的 dd"),
    ("echo 'mkfs /dev/sda'", "单引号里的 mkfs"),
    ('printf "%s\\n" "reboot"', "引号里的 reboot"),

    # --- 老代码的误伤:子串撞上了无关的词 ---
    ("grep -rn mkfs docs/", "★ 在文档里搜 mkfs 这个词"),
    ("man mkfs.ext4", "看手册"),
    ("cat shutdown.log", "★ 读一个叫 shutdown 的日志"),
    ("grep reboot /var/log/syslog", "日志里搜 reboot"),
    ("git commit -m 'fix mkfs handling'", "commit message 里提到 mkfs"),

    # --- 这些本来就该放行,改完不许变成新的误伤 ---
    ("fdisk -l /dev/sda", "★ 读分区表 —— 往宽了改很容易把这条判成 DENY"),
    ("lsblk", "列块设备"),
    ("cat /dev/sda | head -c 512", "读设备头 —— 只在写目标上判,读不拦"),
    ("cat /dev/urandom | head -c 32", "取随机数"),
    ("ls -la", "普通列目录"),
    ("nmap -sV 10.0.0.1", "侦察"),
    ("curl -s http://10.0.0.5/", "发请求"),
    ("python exploit.py --target 10.0.0.5", "跑脚本"),
    ("echo renaming log", "ASK 改词匹配时就修掉的那种误伤:renaming ≠ ren"),
    ("cat /etc/passwd", "读文件"),

    # --- 嵌套了,但嵌套的那段本身无害 ---
    #★ 这几条是上面那组递归的**反向**用例:证明不是"见到 sh -c 就拦",
    #  而是真把参数当命令解析了。少了它们,递归写成 `if prog in _INTERPRETERS: return DENY`
    #  也照样全绿。
    ('sh -c "echo hi"', "嵌套无害"),
    ("bash -c 'ls -la'", "嵌套的只是列目录"),
    ('sh -c "grep -rn mkfs docs/"', "嵌套里还带着老误伤:search mkfs 不该被拦"),
    ('sh script.sh', "跑脚本不是 -c,参数不是代码"),
    ("eval \"$SHELL_VAR\"", "eval 一个变量 —— 静态看不出来,只能放行"),
    #★ 已知边界,故意留的:python -c 里 os.system("rm -rf /") 静态看不出来。
    #  见文件末尾"这一档拦不住什么"。老代码靠子串**恰好**能拦住 `python -c "rm -rf /"`
    #  这种字面形态,但对 base64 解码后执行之类的同样没办法 —— 这类不是静态分析能闭合的。
    ('python3 -c "print(1)"', "python 不是 shell,不递归"),
]


#============================================================
# 跑
#============================================================

def _fmt(p):
    return p.name


def _check(cases, expected, failures):
    for command, why in cases:
        got = check_bash_permission(command)
        if got != expected:
            failures.append(
                f"  {command!r}\n      期望 {expected.name},实得 {_fmt(got)}   ({why})")


def test_deny_cases():
    failures = []
    _check(DENY_CASES, Permission.DENY, failures)
    assert not failures, "该拒的没拒:\n" + "\n".join(failures)


def test_ask_cases():
    failures = []
    _check(ASK_CASES, Permission.ASK, failures)
    assert not failures, "该问的没问:\n" + "\n".join(failures)


def test_allow_cases():
    failures = []
    _check(ALLOW_CASES, Permission.ALLOW, failures)
    assert not failures, "该放行的被拦了(误伤):\n" + "\n".join(failures)


#本次**有意**从 DENY 降到 ALLOW 的。每一条都是子串匹配的误伤:
#老代码拦它不是因为危险,而是因为那条命令的**文本里恰好含有一段危险字符串**。
#往这个集合里加东西之前请想清楚:那等于承认"我们要少拦一条",得有理由。
INTENDED_DENY_TO_ALLOW = {
    # 引号里的东西是死的 —— shell 不会执行它
    'echo "> /dev/sda"': "引号里的重定向只是要打印的文本",
    'echo "rm -rf /"': "引号里的删根命令只是文本",
    'echo "dd if=x"': "引号里的 dd 参数",
    "echo 'mkfs /dev/sda'": "单引号里的 mkfs",
    'printf "%s\\n" "reboot"': "引号里的 reboot",

    # 子串撞上了无关的词
    "grep -rn mkfs docs/": "在文档里搜 mkfs 这个词",
    "man mkfs.ext4": "在看 mkfs 的手册",
    "cat shutdown.log": "读一个恰好叫 shutdown 的日志",
    "grep reboot /var/log/syslog": "在日志里搜 reboot",
    "git commit -m 'fix mkfs handling'": "commit message 里提到 mkfs",
    # 套了一层 sh -c 的同一条误伤。它出现在这儿是回归门**逼**出来的,不是我主动想到的:
    # 加嵌套用例时我只加了危险的那几条,是 test_no_old_deny_became_allow 报出来
    # "这条老代码也拒了,现在放行了,要么改回去要么写理由"。理由和上面那条完全一样 ——
    # 串里出现 mkfs 不代表要格式化。递归把误伤也一起继承了,这是对的。
    'sh -c "grep -rn mkfs docs/"': "在嵌套里搜 mkfs 这个词,同上面那条",
}

#本次从 DENY 降到 ASK 的。★ 降级不等于放行 —— ASK 仍然要人来点头
#(无人值守时由 HOOKS.py 按拒绝处理)。但它确实比 DENY 松,所以必须逐条写明理由。
REVIEWED_DENY_TO_ASK = {
    "rm -rf /tmp/scan": "★ 本次要修的核心:子串 \"rm -rf /\" 撞上了清 /tmp",
    "rm -rf /var/log/old": "同一类:清 /var/log 下的子目录",
    "rm -rf /tmp/a /tmp/b": "同一类:一次清多个临时目录",
    "docker run alpine rm -rf /data": "程序词是 docker,删的是容器内路径;ASK 仍会问",
    # 套 shell 的同一条:老代码靠 "rm -rf /" 子串判 DENY,其实删的是 /tmp。
    # 递归之后正确定位到"目标是 /tmp 不是根" → 落回 ASK,和没套 shell 的版本一致。
    'sh -c "rm -rf /tmp/scan"': "同 rm -rf /tmp/scan:嵌在 sh -c 里也是清临时目录,不该判删根",
}


def _corpus():
    """拿来比对新老行为的命令集。

    = 三类用例里的全部 + 一批"只在这组测试里出现"的形态(免得上面的白名单
      变成唯一防线 —— 白名单只该为**已知原因**开口子,不该替未知情况兜底)
    """
    return [c for c, _ in DENY_CASES + ASK_CASES + ALLOW_CASES] + [
        "rm -rf /tmp/a /tmp/b", "cp -r src dst", "dd if=a",
        "echo done > /dev/sda", "sudo shutdown now", "mkfs -t ext4 /dev/sdb",
    ]


def test_no_old_deny_became_allow():
    """★ 这个文件里最重要的一组:老代码拒掉的东西,不许变成 ALLOW。

    没有例外名单 —— ALLOW 就是放行,放行一条危险命令就是漏洞。
    老代码因为子串误伤而拒掉的,正确的去向是 ASK(仍要问人)或明确记在
    INTENDED_DENY_TO_ALLOW 里,而不是悄悄变成 ALLOW。
    """
    lost = [c for c in _corpus()
            if _old_check_bash_permission(c) == Permission.DENY
            and c not in INTENDED_DENY_TO_ALLOW
            and check_bash_permission(c) == Permission.ALLOW]

    assert not lost, (
        "老代码拒掉、新代码直接放行了(这是漏洞,不是改进):\n  "
        + "\n  ".join(lost)
        + "\n\n如果确实该放行,把它连同**理由**加进 INTENDED_DENY_TO_ALLOW。")


def test_deny_to_ask_only_for_reviewed_cases():
    """从 DENY 降到 ASK 的每一条都必须事先review过,并且写明理由"""
    unreviewed = [c for c in _corpus()
                  if _old_check_bash_permission(c) == Permission.DENY
                  and c not in INTENDED_DENY_TO_ALLOW
                  and check_bash_permission(c) == Permission.ASK
                  and c not in REVIEWED_DENY_TO_ASK]

    assert not unreviewed, (
        "老代码 DENY、新代码降成 ASK 了,而且没登记理由:\n  "
        + "\n  ".join(unreviewed)
        + "\n\n降级不等于放行(ASK 仍要人点头),但确实更松 —— "
          "确认过就加进 REVIEWED_DENY_TO_ASK 并写明为什么。")


def test_review_lists_are_not_stale():
    """白名单里不该留已经不成立的条目 —— 过期条目会让这个门形同虚设"""
    stale = []
    for command in INTENDED_DENY_TO_ALLOW:
        if _old_check_bash_permission(command) != Permission.DENY:
            #这条本来新老都是 ALLOW,列在这里说明它压根不是"老代码的误伤"
            if check_bash_permission(command) == Permission.ALLOW:
                stale.append(f"{command!r}: 老代码本来就没拒它")
    for command in REVIEWED_DENY_TO_ASK:
        if check_bash_permission(command) != Permission.ASK:
            stale.append(f"{command!r}: 现在不在 ASK 这一档了,该从名单里删掉")
    assert not stale, "白名单里的过期条目:\n  " + "\n  ".join(stale)


def test_no_old_ask_was_lost():
    """ASK 那一档不许有任何回退 —— 它这次压根没改,只是不许被新版误伤"""
    corpus = [c for c, _ in DENY_CASES + ASK_CASES + ALLOW_CASES]
    corpus += ["cp -r src dst", "mv a b", "rmdir x", "ren a b", "del x",
               "format c:", "move a b"]

    lost = []
    for command in corpus:
        if _old_check_bash_permission(command) == Permission.ASK:
            if check_bash_permission(command) == Permission.ALLOW:
                lost.append(command)
    assert not lost, ("老代码 ASK、新代码放行:\n  " + "\n  ".join(lost))


def test_no_bypass_through_prefix_or_nesting():
    """★ 交叉生成的绕过扫描 —— 这个文件里最有价值的一组。

    上面那些用例是我**手写**的,手写就会漏。这一段不是:它把
    「危险命令 × 包装器前缀 × 解析器前缀 × 引号形态」全叉乘一遍,逐条要求
    "危险的东西只要真的落在可执行位置上,就不许是 ALLOW"。

    加它是因为手写用例真的漏了一整个类别:改完子串 bug 之后,`sh -c "rm -rf /"`
    会从 DENY 变成 ALLOW —— 老代码的 `"rm -rf /" in command` 顺手盖住了它,
    而"只看程序词"的写法看不见引号里面。手写用例当时一条都没覆盖到。
    """
    from PERMISSIONS import (_MAX_NEST_DEPTH, _nested_code, _program,
                             _split_simple_commands, _tokens)

    def all_programs(command, depth=0):
        """递归收集这条命令里所有**真会被当程序跑**的程序词"""
        if depth >= _MAX_NEST_DEPTH:
            return set()
        found = set()
        for part in _split_simple_commands(command):
            tokens = _tokens(part)
            if not tokens:
                continue
            prog, args = _program(tokens)
            if prog:
                found.add(prog)
            nested = _nested_code(prog, args)
            if nested:
                found |= all_programs(nested, depth + 1)
        return found

    dangerous = ["rm -rf /", "rm -rf //", "rm -rf /*", "rm -rf /.*", "rm -fr /",
                 "rm -rf /--no-preserve-root", "mkfs.ext4 /dev/sdb1", "mkfs /dev/sda1",
                 "mke2fs /dev/sdb1", "dd if=/dev/zero of=/dev/sda", "dd if=/dev/sda of=x",
                 "cat img > /dev/sda", "cat img >/dev/sda", "shutdown -h now",
                 "reboot", "halt", "poweroff"]
    # 真正危险的那些程序名。"cat"/"echo" 不在内 —— 它们只有配上 `> 裸设备` 才危险,
    # 而那种情况程序词是 cat/echo,单看程序名分不出来,交给 DENY_CASES 里的定向用例。
    danger_prog = {"rm", "mkfs", "mkfs.ext4", "mke2fs", "dd",
                   "reboot", "halt", "poweroff", "shutdown"}
    wrappers = ["", "sudo ", "doas ", "nohup ", "nice -n 10 ", "env X=1 ", "time ",
                "command ", "busybox ", "xargs ", "stdbuf -o0 ", "setsid ", "exec ",
                "ionice -c2 ", "sudo -u root "]
    #★ 后两个是**引号没配对**的畸形形态:shlex 切不动、走降级路径。
    #  真实 shell 会直接报语法错什么都不执行,所以它们本身是死的 ——
    #  但"我们的解析器在这里失灵了"是真的,所以按读不懂处理(ASK)。
    interps = ["", "sh -c ", "bash -c ", "su -c ", "eval ", "zsh -c ", "sudo sh -c ",
               "busybox sh -c ", "sh -c 'sh -c ", "sh -c 'sh -c \"sh -c "]

    leaks = []
    checked = 0
    for bad in dangerous:
        for wrap in wrappers:
            for interp in interps:
                for quote in ("{}", '"{}"', "'{}'"):
                    command = wrap + interp + quote.format(bad)
                    checked += 1
                    if check_bash_permission(command) != Permission.ALLOW:
                        continue
                    #落到 ALLOW 了 —— 只有当危险的东西**真的会被执行**时才算漏
                    if all_programs(command) & danger_prog:
                        leaks.append(command)

    assert checked > 5000, f"叉乘规模不对,只生成了 {checked} 条"
    assert not leaks, (
        f"以下 {len(leaks)} 条命令,危险的程序真的会被执行,却被放行了:\n  "
        + "\n  ".join(sorted(set(leaks))[:20])
        + "\n\n这是绕过通道,不是误伤。包装器/解析器前缀必须能穿透。")


def test_fail_closed_on_unreadable_commands():
    """读不懂的命令落 ASK,不许落 ALLOW。

    这条守的是 PERMISSIONS.py 里 _unidentified 那段:程序词认不出来(全被包装器/
    开关/赋值吃掉)、或者 shlex 压根切不动走了降级路径 —— 这两种都说明解析器没吃透。
    "没吃透"不等于"安全"。
    """
    from PERMISSIONS import _unidentified, _program, _tokens

    # 程序词认不出来的几种
    for command in ['sudo "rm -rf /"', "sudo -u root", "nice -n 10", "sudo", "env",
                    "command ", "xargs -0", "stdbuf -o0"]:
        assert _program(_tokens(command))[0] == "", f"{command!r} 应该认不出程序词"
        assert _unidentified(command) is not None, f"{command!r} 应该被标记为读不懂"
        assert check_bash_permission(command) == Permission.ASK, \
            f"{command!r} 读不懂时应该 ASK,不能放行"

    # 引号不配对 → shlex 切不动 → 降级路径 → 也算读不懂
    for command in ["sh -c 'sh -c \"rm -rf /\"", 'echo "unclosed', "rm -rf /'"]:
        assert _unidentified(command) is not None, f"{command!r} 降级路径应该被标记"

    # 反例:纯赋值是一条完整、无副作用的命令,不该被当成读不懂
    assert _unidentified("X=1") is None
    assert _unidentified("X=1 Y=2") is None
    assert check_bash_permission("X=1") == Permission.ALLOW

    # 反例:正常命令当然认得出来
    for command in ["ls -la", "nmap -sV 10.0.0.1", 'echo "rm -rf /"', "fdisk -l /dev/sda"]:
        assert _unidentified(command) is None, f"{command!r} 不该被当成读不懂"
        assert check_bash_permission(command) == Permission.ALLOW


def test_split_and_program_helpers():
    """拆命令和认程序词这两个底层函数单独测 —— 上面全绿但这两个坏了,
    说明只是用例没覆盖到那条路径"""
    from PERMISSIONS import _program, _resolves_to_root, _split_simple_commands

    assert _split_simple_commands("a && b; c | d") == ["a", "b", "c", "d"]
    assert _split_simple_commands('echo "a && b"') == ['echo "a && b"'], \
        "引号里的 && 不该被当分隔符"
    assert _split_simple_commands("echo 'x; y' ; z") == ["echo 'x; y'", "z"]

    assert _program(["rm", "-rf", "/"]) == ("rm", ["-rf", "/"])
    assert _program(["sudo", "rm", "-rf", "/"]) == ("rm", ["-rf", "/"])
    assert _program(["sudo", "-u", "root", "rm"])[0] == "rm", "sudo 的参数没跳干净"
    assert _program(["nice", "-n", "10", "rm", "-rf", "/"])[0] == "rm"
    assert _program(["X=1", "Y=2", "rm"])[0] == "rm"
    assert _program(["/usr/bin/rm", "-rf", "/"])[0] == "rm"
    assert _program(["VAR=x", "sudo", "rm"])[0] == "rm"
    assert _program([]) == ("", [])

    for root_like in ["/", "//", "/*", "/.*", "/./", "/../", "/*/..", "/***", "/??"]:
        assert _resolves_to_root(root_like), f"{root_like} 应该算根"
    for not_root in ["/tmp", "/var/log", "/root", "/etc/*", "/dev/sda", "/tmp/x",
                     "~", "$HOME", "", "*", "./x"]:
        assert not _resolves_to_root(not_root), f"{not_root} 不该算根"


def test_nested_code_and_command_words():
    """_nested_code / _command_words —— 递归那两条路径的底层函数。

    ★ 这里钉的是"谁在执行引号里的东西"这条区分。判错的后果是不对称的:
      判宽 → `echo "rm -rf /"` 被误伤(吵,但安全);
      判窄 → `sh -c "rm -rf /"` 放行(真绕过)。
      所以下面把两个方向都写死了。
    """
    from PERMISSIONS import _command_words, _nested_code, _program, _tokens

    def nested_of(command):
        prog, args = _program(_tokens(command))
        return _nested_code(prog, args)

    # 会执行参数的 —— 引号里的是**活的**
    assert nested_of('sh -c "rm -rf /"') == "rm -rf /"
    assert nested_of("bash -c 'ls -la'") == "ls -la"
    assert nested_of('zsh -c "rm -rf /"') == "rm -rf /"
    assert nested_of("sudo sh -c 'rm -rf /'") == "rm -rf /", "包装器后面也要能穿进去"
    assert nested_of("busybox sh -c 'rm -rf /'") == "rm -rf /"
    assert nested_of('su -c "rm -rf /" root') == "rm -rf / root", "su -c 后面的才是命令"
    assert nested_of('su -s /bin/bash -c "rm -rf /"') == "rm -rf /", "-c 不在第一个位置也要找到"
    assert nested_of('eval "rm -rf /"') == "rm -rf /"
    assert nested_of('cmd /C "del x"') == "del x", "cmd 的 /C 不区分大小写"

    # 不执行参数的 —— 引号里的是**死的**。这一组和上面那组同等重要
    assert nested_of('echo "rm -rf /"') is None
    assert nested_of('printf "%s" "rm -rf /"') is None
    assert nested_of("grep -rn 'rm -rf /' .") is None
    assert nested_of("python3 -c 'print(1)'") is None, "python 不是 shell,不递归"

    # 不带 -c 的解析器:普通的跑脚本,参数不是代码
    assert nested_of("sh script.sh") is None
    assert nested_of("bash -x script.sh") is None

    # _command_words:ASK 那一档靠它把词表扩到嵌套里去
    assert "rm" in _command_words('sh -c "rm -rf /tmp/x"'), \
        "★ 不递归取词的话,套一层 sh -c 就能跳过 ASK 确认"
    assert "rm" in _command_words("docker run alpine rm -rf /data"), "原串切词那条路要保留"
    assert "rm" in _command_words("find . -exec rm {} ;")
    assert "rm" not in _command_words('echo "rm -rf /"'), "echo 里的 rm 是死的,不该触发 ASK"
    assert "rm" not in _command_words('sh -c "echo hi"')

    # 套娃必须有底(权限检查跑在同步路径上,不能被构造出来的长命令拖死)
    deep = 'sh -c "' * 20 + "rm -rf /" + '"' * 20
    check_bash_permission(deep)                  # 不抛异常即可


def test_never_raises():
    """权限检查自己崩了 = 这次调用没有权限检查。任何输入都不许抛异常"""
    weird = [
        "", "   ", "rm -rf /'", 'echo "unclosed', "\\\\", ">", ">>>",
        "rm -rf / &&", "&&", "|", "\n\n", "rm\x00-rf", "sudo", "env",
        "a" * 5000, "rm -rf " + "/" * 500,
    ]
    for command in weird:
        try:
            result = check_bash_permission(command)
        except Exception as e:                       # noqa: BLE001
            raise AssertionError(f"{command!r} 抛了 {type(e).__name__}: {e}")
        assert result in (Permission.ALLOW, Permission.ASK, Permission.DENY)


#============================================================
# 不装 pytest 也能直接跑
#============================================================

def _main():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {t.__name__}\n{e}")
    print()
    print(f"{len(tests) - failed}/{len(tests)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_main())
