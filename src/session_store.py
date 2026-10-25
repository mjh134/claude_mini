"""会话持久化(ROADMAP 项 9):主对话 history 的落盘与恢复。

设计要点(见 doc/TASK.md 项 9 任务卡):
- 存 wire-format dict,不还原 SDK 对象:API 本来就接受 JSON 块;run() 对 history
  旧条目只透传(压缩/计数/发送),只有新 response 走块对象属性——还原对象徒增耦合
- 写点在 run_turn 正常返回后(由调用方保证):那是 tool_use 与 tool_result 成对、
  压缩完成的点,盘上永远合法 → 加载零修复分支(关键约束 1)
- 降级不崩:保存失败只警告不阻断对话;坏 JSON 改名 .bad 留证,下次不再撞同一堵墙
"""
import json
from pathlib import Path

import ui


def _normalize(x):
    """把消息里混着的 SDK 块对象递归转成 JSON 可序列化的纯结构。

    history 里三类内容:纯 str、dict(消息本体/tool_result)、SDK 块对象
    (assistant 的 content 列表元素)。只有第三类需要 model_dump(),
    前两类原样递归——一个开关打遍全树,不用按 role 分别处理。
    """
    if hasattr(x, "model_dump"):
        return x.model_dump()
    if isinstance(x, list):
        return [_normalize(i) for i in x]
    if isinstance(x, dict):
        return {k: _normalize(v) for k, v in x.items()}
    return x


def save_session(history, path):
    """把主对话 history 全量覆盖写到 path。失败只警告:存档是恢复面,不能挂掉对话。"""
    try:
        data = [_normalize(m) for m in history]
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except (OSError, TypeError, ValueError) as e:
        ui.warn(f"[session] 会话存档写不进去(退出后无法 --resume):{type(e).__name__}: {e}")


def _discard_corrupt(p, why):
    """坏存档改名 .bad 留证,免得每次启动都撞同一堵墙;失败就留着,下次再报。"""
    ui.warn(f"[session] 会话存档 {p.name} {why},放弃恢复(改名留证)")
    try:
        p.rename(p.with_name(p.name + ".bad"))
    except OSError:
        pass


def load_session(path):
    """读回 history。成功返回消息列表;无文件/坏 JSON/形状不对返回 None(调用方开新会话)。"""
    p = Path(path)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        _discard_corrupt(p, f"读不出来({type(e).__name__}: {e})")
        return None
    if not isinstance(data, list):
        _discard_corrupt(p, "形状不对(不是消息列表)")
        return None
    return data
