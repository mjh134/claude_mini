import json
import os
import re
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

import paths
import ui

# 命中即替换的最小指纹长度——太短的值(如 KEY=1)会误伤正常文本
SECRET_MIN_LEN = 6
# 脱敏按 key 名判定:凭据类才指纹化。MODEL_ID/目录名这类配置值不是凭据,
# 全遮会把 trace 里的路径/模型名打成乱码,观测价值直接报废
SECRET_KEY_RE = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH)", re.I)
# 兜底正则：没进 .env 但长得像密钥的也遮掉
SECRET_PATTERNS = [re.compile(r"sk-[A-Za-z0-9_\-]{10,}")]
# 超长字符串截断。全文另有 tool_result/ 落盘，trace 只留现场
STR_MAX_LEN = 200


class Span:
    """span 的事件壳子：with 块里往里补字段，退出时整体落盘"""

    def __init__(self):
        self.fields = {"ok": True}

    def set(self, key: str, value) -> None:
        self.fields[key] = value


class Trace:

    #trace_dir/secrets 是给测试的注入口(临时目录 + 假指纹表);生产走 get_trace() 不带参
    def __init__(self, trace_dir=None, secrets=None):
        #TRACE_DIR 是沙箱钩子:实验的 trace 落自己目录里,不混进真实 trace/(决策见 decisions/0006)
        load_dotenv()
        self.dir = Path(trace_dir) if trace_dir else paths.PROJECT_ROOT / os.getenv("TRACE_DIR", "trace")
        self.path = self.dir / f"sess-{datetime.now():%Y%m%d-%H%M%S}.jsonl"
        self.lock = threading.Lock()
        self.seq = 0
        self.enabled = True
        self.secrets = list(secrets) if secrets is not None else self.load_secrets()
        try:
            self.dir.mkdir(exist_ok=True)
            self.fh = self.path.open("w", encoding="utf-8")
        except OSError as e:
            # 观测面建不起来不影响主链路——降级成 no-op，只说一次
            self.enabled = False
            ui.status(f"[trace] 无法写入 {self.path}（{e}），观测降级为 no-op")

    def emit(self, event_type: str, payload, *, agent_id: str,
             turn: int = None, correlation_id: str = None) -> None:
        """唯一写入口：组骨架 → 脱敏 → 加锁追加一个事件。
        失败只告警并关闭观测，绝不上抛——trace 炸不许带崩对话。
        turn 可空：任务级事件(task_runner)不属于任何一轮 react 循环。"""
        if not self.enabled:
            return
        event = {
            "ts": time.time(),
            "agent_id": agent_id,
            "turn": turn,
            "type": event_type,
            "correlation_id": correlation_id,
            "payload": self.sanitize(payload),
        }
        try:
            with self.lock:
                # seq 的取号和写入必须在同一把锁里，并发时才不会撞号
                event["seq"] = self.seq
                self.seq += 1
                #缩进多行纯粹为人眼服务。文件仍是"JSON 文档串接"的 append-only 流,
                #解析端用 raw_decode 顺序读(对本文件的一行式老格式同样适用)。
                #代价:kill 可能留半个事件在末尾——但截断点只会在最后,前面全部完整
                self.fh.write(json.dumps(event, ensure_ascii=False,
                                         default=str, indent=2) + "\n\n")
                self.fh.flush()
        except Exception as e:
            self.enabled = False
            ui.status(f"[trace] 写入失败（{e}），停止观测——主链路不受影响")

    @contextmanager
    def span(self, event_type: str, *, agent_id: str, turn: int = None,
             correlation_id: str = None):
        """"有始有终"型埋点：块里补字段，退出时自动落一条，含耗时和成败。
        异常路径同样记账（记完原样上抛），失败现场正是观测最值钱的记录。"""
        s = Span()
        start = time.time()
        try:
            yield s
        except Exception as e:
            s.set("ok", False)
            s.set("error", f"{type(e).__name__}: {e}")
            raise
        finally:
            s.set("elapsed", round(time.time() - start, 3))
            self.emit(event_type, s.fields, agent_id=agent_id,
                      turn=turn, correlation_id=correlation_id)

    def sanitize(self, obj):
        """递归脱敏：dict/list 下钻，字符串过指纹和正则。
        所有落盘内容必经此处——契约，无旁路。"""
        if isinstance(obj, dict):
            return {k: self.sanitize(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [self.sanitize(v) for v in obj]
        if isinstance(obj, str):
            return self.sanitize_text(obj)
        if obj is None or isinstance(obj, (bool, int, float)):
            return obj
        #其余类型(SDK block 对象等)先转文本再过脱敏——否则 json.dumps 的
        #default=str 会在 sanitize 之后才把它们拼进 JSON,绕过整道防线(e2e 实测踩过)
        return self.sanitize_text(str(obj))

    def sanitize_text(self, text: str) -> str:
        for key, value in self.secrets:
            if value in text:
                text = text.replace(value, f"[REDACTED:{key}]")
        for pat in SECRET_PATTERNS:
            text = pat.sub("[REDACTED:pattern]", text)
        if len(text) > STR_MAX_LEN:
            text = text[:STR_MAX_LEN] + f"...[截断 {len(text) - STR_MAX_LEN} 字符]"
        return text

    def load_secrets(self, env_file=None):
        """启动时读一次 .env 建指纹表。按 value 长度降序——防止短指纹先命中、
        把长指纹的局部遮成残缺标记。运行中新写入的密钥不追（见 ADR）。"""
        env_file = Path(env_file) if env_file else paths.PROJECT_ROOT / ".env"
        found = []
        if not env_file.exists():
            return found
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if "=" not in line or line.strip().startswith("#"):
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip("'\"")
            if len(value) >= SECRET_MIN_LEN and SECRET_KEY_RE.search(key):
                found.append((key, value))
        found.sort(key=lambda kv: len(kv[1]), reverse=True)
        return found

    def close(self) -> None:
        if self.enabled:
            with self.lock:
                self.fh.close()


instance = None
init_lock = threading.Lock()


def get_trace() -> Trace:
    """进程级单例：所有身份共用一个 writer，文件里靠 agent_id 分轨。"""
    global instance
    if instance is None:
        with init_lock:
            if instance is None:
                instance = Trace()
    return instance