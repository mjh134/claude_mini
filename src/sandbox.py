"""沙箱：把一局实验的环境物化成一个目录，用 cwd + 环境变量交给 agent。

    python src/main.py --sandbox sandbox/dry        #不存在则先建骨架再复用
    import sandbox; sandbox.create(Path("sandbox/dry"))

只负责装环境，不提供隔离：PROJECT_ROOT 仍指向真实仓库（决策与缺口见 decisions/0006）。
"""
import hashlib
import os
import re
import shutil
from datetime import datetime
from pathlib import Path

import paths

OVERLAY_FILE = "PROMPT_OVERLAY.md"
IDENTITY_FILE = "PROMPT_IDENTITY.md"   #可选：非空则顶替内置身份切片（_IDENTITY_MAIN）
MANIFEST_FILE = "sandbox.json"
CASE_FILE = "case.yaml"               #声明式实验蓝图：有它则启动前自动物化（需 PyYAML）
MEMORY_DIR = "memory"
SKILL_DIR = "SKILLS"
TOOL_RESULT_DIR = "tool_result"
TRACE_DIR = "trace"
TRANSCRIPT_FILE = "transcript"

#环境变量 → 沙箱内的相对位置。这是"目录怎么变成 env"的唯一出口，加构件只动这一处 + _SKELETON。
#env() 把它们解析成**绝对路径**再交出去：MEMORY_DIR/SKILL_DIR/TRACE_DIR 在模块里都是
#PROJECT_ROOT / 名字 的形式解析的（paths.py 钉死的锚定纪律），传相对名等于指回真实项目 ——
#实测踩过：传 "memory" 拿到的是项目根的 memory/
_ENV_MAP = [
    ("MEMORY_DIR", MEMORY_DIR),
    ("SKILL_DIR", SKILL_DIR),
    ("TOOL_RESULT_PATH", TOOL_RESULT_DIR),
    ("TRANSCRIPT", TRANSCRIPT_FILE),
    ("TRACE_DIR", TRACE_DIR),
    ("PROMPT_OVERLAY_FILE", OVERLAY_FILE),
    ("PROMPT_IDENTITY_FILE", IDENTITY_FILE),
]

#骨架:目录 + 空装弹文件。空文件写 0 字节 —— overlay 为空时提示词逐字节不变
_SKELETON_DIRS = [
    MEMORY_DIR + "/long_term", SKILL_DIR, TOOL_RESULT_DIR, TRACE_DIR, ".task",
]
_SKELETON_FILES = [
    OVERLAY_FILE,
    IDENTITY_FILE,
    MEMORY_DIR + "/long_term/user.md",
    MEMORY_DIR + "/long_term/soul.md",
    MEMORY_DIR + "/long_term/project.md",
]


def create(root):
    """建骨架 + 记账，返回沙箱根。**已存在的文件一律不动**——改装弹是主要用法，
    重复 create 不能把埋好的弹毁掉。"""
    root = Path(root)
    _reject_project_root(root)
    for rel in _SKELETON_DIRS:
        (root / rel).mkdir(parents=True, exist_ok=True)
    for rel in _SKELETON_FILES:
        target = root / rel
        if not target.exists():
            target.write_text("", encoding="utf-8")
    manifest = root / MANIFEST_FILE
    if not manifest.exists():
        manifest.write_text(
            f'{{"created": "{datetime.now():%Y-%m-%d %H:%M}", "isolation": "none"}}',
            encoding="utf-8")
    return root


def env(root):
    """沙箱目录 → 环境变量字典（绝对路径）。

    绝对路径是必要的而不是偷懒:位置类变量在模块里都按 PROJECT_ROOT / 名字 解析,非绝对就指回真实项目。
    代价是跨盘符时 pathlib 会丢掉左侧(Path("C:/x") / "D:/y" 得到的是相对路径),
    所以 check_env() 要查沙箱与项目根是否同盘。
    """
    root = Path(root)
    create(root)
    return {name: str((root / rel).resolve()) for name, rel in _ENV_MAP}


#运行态产物（重跑实验前要清的）：trace/ tool_result/ .task/。
#配置（overlay/identity/memory/SKILLS/case.yaml）绝不在这里 —— fresh_clean 只擦运行态。
_RUNTIME_DIRS = [TRACE_DIR, TOOL_RESULT_DIR, ".task"]


def load_case(root):
    """读 root/case.yaml（声明式实验蓝图），返回 dict；目录里没这个文件 → None（退回手动铺文件）。
    内容为空 → {}。解析错误**直接抛**（声明写错就该当场知道，不像缺文件那样静默降级）。
    依赖 PyYAML（本机已装）；延迟到真要读时才 import，保住 sandbox 其余部分的零三方依赖。
    """
    path = Path(root) / CASE_FILE
    if not path.is_file():
        return None
    import yaml
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def materialize(root, case):
    """把 case.yaml 声明的内容落成沙箱文件 —— 一份声明 = 一局环境。

    与 create 相反：create 继不覆盖已存在文件（守手埋的弹）；materialize **以 case 为准覆写**
    它声明的那几格，因为 case 是这一局环境的唯一真相、重跑要能重置。只碰声明了的键。
    便利字段 identity/overlay/memory/skills 各映射到固定路径；files 是逃生舱（任意相对路径→内容）。
    """
    root = Path(root)
    create(root)                                  #先保骨架（目录与默认空文件）

    def _write(rel, text):
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    if case.get("identity") is not None:
        _write(IDENTITY_FILE, str(case["identity"]))
    if case.get("overlay") is not None:
        _write(OVERLAY_FILE, str(case["overlay"]))
    for key, body in (case.get("memory") or {}).items():
        _write(f"{MEMORY_DIR}/long_term/{key}.md", str(body))
    for name, body in (case.get("skills") or {}).items():
        _write(f"{SKILL_DIR}/{name}/SKILL.md", str(body))
    for rel, body in (case.get("files") or {}).items():
        _write(str(rel), str(body))               #逃生舱：任意沙箱内相对路径
    return root


def fresh_clean(root):
    """只清运行产物（trace/ tool_result/ .task/ 与 transcript），配置（装弹/case）一个不碰。
    清完把这几类目录重建为空，好让 check_env 的骨架检查仍过。让“重跑同一实验”零手工又不毁弹。
    """
    root = Path(root)
    for rel in _RUNTIME_DIRS:
        target = root / rel
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
        target.mkdir(parents=True, exist_ok=True)
    (root / TRANSCRIPT_FILE).unlink(missing_ok=True)
    return root


def fingerprint(root):
    """overlay + 长期记忆 + skill 的内容指纹（路径也参与：换个文件名算换环境）。
    十几个字的成本，换"那一局确实是这版装弹"的可复现证据。"""
    root = Path(root)
    digest = hashlib.sha256()
    blobs = []
    for rel in _SKELETON_FILES:
        target = root / rel
        if target.is_file():
            blobs.append((rel, target.read_bytes()))
    for target in sorted(root.glob(SKILL_DIR + "/**/*")):
        if target.is_file():
            blobs.append((str(target.relative_to(root)).replace("\\", "/"), target.read_bytes()))
    for rel, data in sorted(blobs):
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(data)
    return digest.hexdigest()[:12]


def check_env(source_dir=None, root=None):
    """自检 sandbox.env() 的映射表和源码里读的环境变量对不对得上，返回问题清单、**绝不抛异常**。

    漏一个构件 = 那一局实验悄悄用了真实环境，出的是假数据；这和"提示词教它调一个它没有的
    工具"是同一类安静故障，所以照 PROMPT.check_prompts / TOOLS.check_roles 的办法查。
    """
    source_dir = Path(source_dir) if source_dir else paths.PROJECT_ROOT / "src"
    read_keys = set()
    for py in sorted(source_dir.glob("*.py")):
        read_keys.update(re.findall(r"os\.getenv\(\s*[\"']([A-Z0-9_]+)[\"']",
                                    py.read_text(encoding="utf-8", errors="replace")))
    mapped = {name for name, _ in _ENV_MAP}
    problems = []
    missing = sorted({k for k in read_keys if _is_positional(k)} - mapped)
    if missing:
        problems.append(f"源码读了位置类环境变量 {missing}，但 sandbox.env() 不覆盖它们 —— "
                        f"沙箱里这些会静默用真实项目的值")
    dead = sorted(mapped - read_keys)
    if dead:
        problems.append(f"sandbox.env() 映射了 {dead}，但没有任何模块读它 —— 映射表失效")
    for name, rel in _ENV_MAP:
        #transcript 是压缩时才写的产物，本来就不该预先存在 —— 只查骨架里该建出来的那些
        if rel != TRANSCRIPT_FILE and root and not (Path(root) / rel).exists():
            problems.append(f"{name} 指向 {root / rel}，但那个路径不在 —— 骨架与映射表对不上")
    if root:
        sandbox_root = Path(root).resolve()
        if sandbox_root.drive and sandbox_root.drive != paths.PROJECT_ROOT.drive:
            problems.append(f"沙箱在 {sandbox_root.drive}、项目在 {paths.PROJECT_ROOT.drive}："
                            "PROJECT_ROOT 拼绝对路径跨盘会退成相对路径，位置类变量会指回 cwd")
    return problems


def _is_positional(key):
    """位置类变量：目录/文件路径。限额、阈值、密钥不算——它们不该随沙箱变（v1 边界）"""
    return key.endswith(("_DIR", "_PATH")) or key in ("TRANSCRIPT", "PROMPT_OVERLAY_FILE", "PROMPT_IDENTITY_FILE")


def _reject_project_root(root):
    """指到真实项目根 = 从此用的就是真实记忆库，装弹看着生效其实没生效。当场拦下。"""
    try:
        resolved = root.resolve()
    except OSError as e:
        raise ValueError(f"沙箱路径打不开：{root}（{e}）")
    if resolved == paths.PROJECT_ROOT.resolve():
        raise ValueError(f"沙箱不能指向项目根 {resolved} —— 那会直接拿真实 memory/、SKILLS/ 当环境")
