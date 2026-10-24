#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""单格评测执行器（子进程）：装配离线 agent → 分幕跑 → 判定 → 写 result.json。

    python eval_runner.py --case <yaml> --trial-dir <dir>

工作目录 = 本格工作区（父进程传入），agent 的相对路径都落在里面。
"""

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

import yaml

import HOOKS
import PERMISSIONS
import paths
import replay
import trace as trace_mod
from claude import ClaudeMini
from corn_job import Scheduler
from mcp import MCPManager
from task import TaskStore
from task_runner import TaskRunner

import eval_judges

FILLER = "The quick brown fox jumps over the lazy dog. data line {n:06d} keep padding\n"


def log(msg):
    print(f"[runner] {msg}", flush=True)


def needle_value(case_id):
    return hashlib.md5(case_id.encode()).hexdigest()[:8]


def gen_text(chars, needle_line=None, at_ratio=None):
    """ASCII 填充文本；needle_line 在 at_ratio(0-1) 处插入一次"""
    lines, size = [], 0
    i = 0
    while size < chars:
        i += 1
        line = FILLER.format(n=i)
        if needle_line and at_ratio and size >= chars * at_ratio:
            lines.append(needle_line)
            size += len(needle_line)
            needle_line = None
        lines.append(line)
        size += len(line)
    return "".join(lines)


def do_setup(case, ws):
    """按 setup 生成工作区，返回模板占位 ctx 与 needle 信息"""
    ctx, needle = {}, None
    spec = case.get("setup") or {}
    for f in spec.get("files") or []:
        target = ws / f["path"]
        target.parent.mkdir(parents=True, exist_ok=True)   #预置文件可能带子目录(如 .task/)
        if f.get("raw"):
            #全文直写:literal 是"往填充文本里插一行",对格式敏感的 fixture(JSON 日志等)
            #会混进填充字符变成坏文件 —— 必须整文件就是 literal 本身,一个字不多
            target.write_text(f["literal"], encoding="utf-8")
            continue
        line, value = None, None
        if f.get("needle"):
            value = needle_value(case["id"])
            line = f"SECRET_TOKEN={value}\n"
        elif f.get("literal"):
            line = f["literal"]
        target.write_text(
            gen_text(f.get("chars", 500), line, f.get("at")), encoding="utf-8")
        if value:
            needle = {"value": value, "file": f["path"], "at": f.get("at")}
    for name, chars in (spec.get("blocks") or {}).items():
        ctx[name] = gen_text(chars)
    if spec.get("files"):
        names = [f["path"] for f in spec["files"]]
        ctx["files_list"] = "\n".join(f"- {n}" for n in names)
    ctx["needle"] = needle["value"] if needle else ""
    return ctx, needle


def render(text, ctx):
    for key, value in ctx.items():
        text = text.replace("{{" + key + "}}", str(value))
    return text


def deep_render(obj, ctx):
    """探针答案/判定参数里也可能引用 {{needle}} 之类的占位"""
    if isinstance(obj, str):
        return render(obj, ctx)
    if isinstance(obj, list):
        return [deep_render(x, ctx) for x in obj]
    if isinstance(obj, dict):
        return {k: deep_render(v, ctx) for k, v in obj.items()}
    return obj


def eval_permission_hook(block):
    """判定仍归 PERMISSIONS，只换 ASK 的处置：无人值守一律明确拒绝并给换法"""
    permission = PERMISSIONS.check_permission(block.name, block.input)
    if permission == PERMISSIONS.Permission.DENY:
        return "❌ Permission denied for the command,不要再尝试执行重复命令"
    if permission == PERMISSIONS.Permission.ASK:
        shown = block.input.get("command") or block.input.get("path") or block.input
        return ("❌ 这条命令需要人工确认，离线评测无人值守，已按拒绝处理。"
                f"请换不需要确认的等价做法（如用 bash 调 python 完成）。被拒对象：{shown}")
    return None


def build_agent(bg_log=None):
    Path(".task").mkdir(exist_ok=True)
    store = TaskStore(".task/eval_tasks.json")
    scheduler = Scheduler(".task/eval_jobs.json")
    mcp = MCPManager(config_path=paths.PROJECT_ROOT / "__eval_no_mcp__.json")
    HOOKS.register_hook("PreToolUse", eval_permission_hook)
    HOOKS.register_hook("BefSubAgent", HOOKS.before_agent_hook)
    HOOKS.register_hook("AftSubAgent", HOOKS.after_agent_hook)
    #bg_log(可选,用例顶层声明):只有测后台任务的格才带日志。TaskRunner 对它做的是
    #显式 opt-in(见 task_runner.py):传了才有"提交落盘/收尾删/启动加载转中断交代",
    #fixture 可以预置遗留文件来模拟"上次进程被强杀"的世界;不传的格零落盘,评测不污染日常日志。
    #drains_results 必须跟着显式 True:显式传 task_runner 后,构造函数不再默认收割邮箱。
    #无日志格传 None:走构造函数默认(=True),与历史行为严格一致,不许动现有格的语义
    runner = TaskRunner(store_path=bg_log) if bg_log else None
    return ClaudeMini(show_thinking=False, silent=False, agent_name="main",
                      task_store=store, scheduler=scheduler, mcp=mcp,
                      task_runner=runner, drains_results=True if runner else None)


def load_events_safely(trace_path):
    try:
        return replay.load_events(trace_path)[0]
    except Exception as e:
        log(f"读 trace 失败：{e}")
        return []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", required=True)
    ap.add_argument("--trial-dir", required=True)
    ap.add_argument("--trial-no", type=int, default=1)   #trial 序号只有父进程知道，显式传进来（别从目录名反推，那是跟命名约定耦合）
    args = ap.parse_args()

    trial_dir = Path(args.trial_dir)
    trial_dir.mkdir(parents=True, exist_ok=True)
    raw_case = yaml.safe_load(Path(args.case).read_text(encoding="utf-8"))
    raw_case["id"] = raw_case.get("id", Path(args.case).stem)
    case = raw_case
    ws = Path.cwd()

    trace_mod.get_trace()                      # 先建实例，保证 trace_path 存在
    started = time.time()
    trial = {"case": case["id"], "suite": case.get("suite", "capability"),
             "trial_no": args.trial_no, "verdict": "error",
             "assertions": [], "answer": "", "needle": None,
             "ws_path": str(ws), "trace_path": str(trace_mod.get_trace().path),
             "error": None}
    ctx = {}

    def finish():
        trial["duration_s"] = round(time.time() - started, 1)
        case = deep_render(raw_case, ctx)
        eval_judges.run_graders(case, trial)
        slim = {k: v for k, v in trial.items() if k != "events"}
        (trial_dir / "result.json").write_text(
            json.dumps(slim, ensure_ascii=False, indent=2), encoding="utf-8")
        log(f"verdict={trial['verdict']} → {trial_dir / 'result.json'}")

    try:
        ctx, needle = do_setup(case, ws)   # 异常时 finish 也要能用，先给默认
        trial["needle"] = needle
        agent = build_agent(case.get("bg_log"))
        history = []

        for i, text in enumerate(case.get("acts") or [], 1):
            log(f"压力幕 {i}/{len(case.get('acts') or [])}")
            history.append({"role": "user", "content": render(text, ctx)})
            agent.run(history)

        # 压力检查只能放在全部幕之后：压力往往是被后面的幕堆出来的
        events = load_events_safely(trial["trace_path"])
        check = eval_judges.judge_pressure(
            {"events": events}, case.get("expect_pressure"))
        if case.get("expect_pressure") and check and not check[0]["ok"]:
            log(f"压力未触发（{check[0]['detail']}），跳过探针——本格未测过")
            trial["events"] = events
            finish()
            return

        mid = case.get("mid_act")
        if mid:
            log(f"幕间改写 {mid['path']}")
            trial["evidence_after_ts"] = time.time()
            (ws / mid["path"]).write_text(mid["content"], encoding="utf-8")

        log("探针幕")
        history.append({"role": "user",
                        "content": render(case["probe"]["say"], ctx)})
        trial["answer"] = agent.run(history) or ""
        trial["events"] = load_events_safely(trial["trace_path"])
    except Exception as e:
        trial["error"] = f"{type(e).__name__}: {e}"
        log(f"异常：{trial['error']}")
        try:
            trial["events"] = load_events_safely(trial["trace_path"])
        except Exception:
            trial["events"] = []
    finish()
    sys.exit(0)


if __name__ == "__main__":
    main()
