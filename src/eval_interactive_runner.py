#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""交互式单格评测执行器：以真实 CLI 入口(python src/main.py, 管道驱动)跑用例。

    python eval_interactive_runner.py --case <yaml> --trial-dir <dir> --trial-no N

与 eval_runner.py 的分工（harness 按用例顶层 runner: interactive 选本执行器）：
  eval_runner               进程内 import ClaudeMini —— 测库层能力，权限 ASK 一律拒绝
  eval_interactive_runner   spawn 真实 main.py 子进程 —— 测真实入口端到端：
                            输入循环、权限弹窗（自动应答）、session 存档、--resume

用例 YAML 与 eval_runner 同格式（acts/probe/graders/budget/setup），交互专属字段：
  runner: interactive            # harness 按它选执行器
  permissions: {default: y}      # 权限弹窗自动应答，缺省 y
  resume: {acts: [...], probe: {...}}   # 第一段 EOF 退出后 spawn --resume 跑第二段；
                                 # answer 取 resume.probe 的回答（没有则取第一段 probe）

判定复用 eval_judges（result.json 同 schema，报告器无感知）。落盘边界：
  cwd = 本格工作区（父进程传入）→ .task/、session.json 全部隔离在工作区里；
  trace 与 memory 走 PROJECT_ROOT 源码锚定 → trace 天然落到项目 trace/（观测数据，
  只读不碍事）；memory 会被 agent 写到真实目录，本执行器跑前备份、跑后恢复。

轮次同步信号：main.py 的 run_turn 收尾必写 <cwd>/.task/session.json，
用 (mtime_ns, size) 变化判一轮完成 —— stdout 的 "User: " 提示符是队列式输入信号
（agent 干活时读线程也会打它），不能当轮次边界（首轮实测踩过）。
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import yaml

import eval_judges
import paths
import replay
from eval_runner import deep_render, do_setup, log, needle_value

SRC = Path(__file__).resolve().parent
ENV = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1", PYTHONUNBUFFERED="1")

PROMPT_MARK = "User:"            #read_input_plain 的 input("User: ") 提示符
ASK_MARK = "(y/n):"              #HOOKS.permission_hook → ui.ask_yes_no 的弹窗尾巴
ASSIST_MARK = "助手"              #ui 打印正文段落的前缀（仅存档用，判定不吃它）


class AgentSession:
    """一个跑着的 main.py 子进程。管道读线程 + session.json 落盘信号同步。"""

    def __init__(self, ws: Path, resume=False):
        args = [sys.executable, str(SRC / "main.py")]
        if resume:
            args.append("--resume")
        self.ws = ws
        self.session_json = ws / ".task" / "session.json"
        self.chunks = []
        self.proc = subprocess.Popen(args, cwd=str(ws), env=ENV,
                                     stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT)
        threading.Thread(target=self._read, args=(self.proc.stdout.fileno(),),
                         daemon=True).start()

    def _read(self, fd):
        while True:
            try:
                data = os.read(fd, 4096)
            except OSError:
                break
            if not data:
                break
            self.chunks.append(data.decode("utf-8", "replace"))

    def text_from(self, start):
        return "".join(self.chunks[start:])

    def _session_sig(self):
        try:
            st = self.session_json.stat()
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    def _write(self, s):
        self.proc.stdin.write((s + "\n").encode("utf-8"))
        self.proc.stdin.flush()

    def wait_ready(self, timeout=120):
        """等首个输入提示符（fresh/resume 启动 + MCP/scheduler 就绪）"""
        deadline = time.time() + timeout
        mark = len(self.chunks)
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError("agent 进程在就绪前退出："
                                   + self.text_from(mark)[-400:])
            if self.text_from(mark).rstrip().endswith(PROMPT_MARK):
                return
            time.sleep(0.3)
        raise RuntimeError(f"等输入提示符超时（{timeout}s）")

    def turn(self, msg, answer_yes=True, timeout=300):
        """发一幕并等本轮 run_turn 收尾（session.json 落盘变化）。返回 stdout 切片。

        弹窗应答有个已知坑（agent 侧结构性 bug，评测侧只能补偿）：
        管道模式下弹窗的 fallback input()（主线程）与读线程的 input("User: ")
        抢同一个 stdin —— 应答有概率被读线程吃掉变成排队消息，主线程弹窗
        无超时永久卡死。信号：应答后切片里冒出新的 "User:" 提示符。
        补喂一次让主线程的 input 拿到应答（若又被读线程吃掉只会多排一条
        无害的 "y" 消息，不影响 state_check 判定）。
        """
        start = len(self.chunks)
        base_sig = self._session_sig()
        self._write(msg)
        deadline = time.time() + timeout
        answered_at = -1
        asks = 0
        refeed = 0
        last_len = 0
        last_progress = time.time()
        while time.time() < deadline:
            t = self.text_from(start)
            if len(t) != last_len:
                last_len = len(t)
                last_progress = time.time()
            recent = t[-300:]
            p = recent.rfind(ASK_MARK)
            if p != -1 and (start + len(t) - len(recent) + p) > answered_at:
                self._write("y" if answer_yes else "n")
                answered_at = start + len(t)
                asks += 1
                log(f"权限弹窗 #{asks} -> 应答 {'y' if answer_yes else 'n'}")
                time.sleep(0.3)
                continue
            #补偿:应答后冒出新输入提示符 = 被读线程吃了,弹窗还挂着 → 补喂(至多 2 次)
            if (answered_at > 0 and refeed < 2
                    and PROMPT_MARK in t[answered_at - start:]
                    and time.time() - last_progress > 8):
                refeed += 1
                self._write("y" if answer_yes else "n")
                answered_at = start + len(t)
                log(f"弹窗应答疑似被读线程吞掉，补喂 #{refeed}")
                time.sleep(0.3)
                continue
            if self._session_sig() != base_sig and self.proc.poll() is None:
                time.sleep(2)          #留出状态行/收尾打印的余量
                return self.text_from(start), asks + refeed
            if self.proc.poll() is not None:
                raise RuntimeError("agent 进程中途退出：" + t[-400:])
            time.sleep(0.5)
        raise RuntimeError(f"一轮等落盘信号超时（{timeout}s），消息：{msg[:40]}")

    def eof(self, timeout=120):
        """stdin 收 EOF（等价 q/Ctrl+D），等优雅收尾完成。"""
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            return self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=10)
            return "KILLED"


def collect_events(ws, since_ts):
    """收本次 trial 期间动过的 trace 文件（resume 会产生第二个进程 = 第二个文件）。"""
    events = []
    trace_dir = paths.PROJECT_ROOT / "trace"
    for p in sorted(trace_dir.glob("sess-*.jsonl")):
        if p.stat().st_mtime >= since_ts - 5:
            try:
                events += replay.load_events(p)[0]
            except Exception as e:
                log(f"读 trace {p.name} 失败：{e}")
    return events


def run_phase(sess, acts, probe, answer_yes, ctx, phase_name, max_minutes):
    """跑一段（acts 逐幕 + probe 幕），返回 probe 的 stdout 切片。"""
    for i, text in enumerate(acts, 1):
        log(f"{phase_name} 压力幕 {i}/{len(acts)}")
        sess.turn(deep_render(text, ctx), answer_yes=answer_yes,
                  timeout=max_minutes * 60 + 60)
    answer_slice = ""
    if probe:
        log(f"{phase_name} 探针幕")
        answer_slice, _ = sess.turn(deep_render(probe["say"], ctx),
                                    answer_yes=answer_yes,
                                    timeout=max_minutes * 60 + 60)
    return answer_slice


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", required=True)
    ap.add_argument("--trial-dir", required=True)
    ap.add_argument("--trial-no", type=int, default=1)
    args = ap.parse_args()

    trial_dir = Path(args.trial_dir)
    trial_dir.mkdir(parents=True, exist_ok=True)
    raw_case = yaml.safe_load(Path(args.case).read_text(encoding="utf-8"))
    raw_case["id"] = raw_case.get("id", Path(args.case).stem)
    case = raw_case
    ws = Path.cwd()

    started = time.time()
    trial = {"case": case["id"], "suite": case.get("suite", "capability"),
             "trial_no": args.trial_no, "verdict": "error",
             "assertions": [], "answer": "", "needle": None,
             "ws_path": str(ws), "trace_path": "", "error": None}

    #agent 子进程会把记忆写到 PROJECT_ROOT/memory（源码锚定，不随 cwd）——
    #跑前备份跑后恢复，评测零污染。备份放系统临时目录，跟工作区彻底分开。
    mem = paths.PROJECT_ROOT / "memory"
    mem_bak = Path(tempfile.mkdtemp(prefix="eval_mem_bak_")) / "memory"
    if mem.exists():
        shutil.copytree(mem, mem_bak)

    raw_log = trial_dir / "session_transcript.log"
    raw_log.parent.mkdir(parents=True, exist_ok=True)
    sess = None
    ctx = {}          #do_setup 异常时 finish 的 deep_render 也要能用

    def finish():
        trial["duration_s"] = round(time.time() - started, 1)
        case_r = deep_render(raw_case, ctx)
        eval_judges.run_graders(case_r, trial)
        slim = {k: v for k, v in trial.items() if k != "events"}
        slim["transcript"] = str(raw_log)
        (trial_dir / "result.json").write_text(
            json.dumps(slim, ensure_ascii=False, indent=2), encoding="utf-8")
        log(f"verdict={trial['verdict']} → {trial_dir / 'result.json'}")

    try:
        ctx, needle = do_setup(case, ws)
        if not ctx.get("needle"):
            #needle 在 eval_runner 里是"往大文件插 SECRET_TOKEN 行"的压缩格用法；
            #e2e 格没有那种文件，但用例模板仍要 {{needle}} 做独特标记 —— 直接补值
            ctx["needle"] = needle_value(case["id"])
        trial["needle"] = needle
        answer_yes = (case.get("permissions") or {}).get("default", "y") == "y"
        minutes = float((case.get("budget") or {}).get("max_minutes", 6))

        sess = AgentSession(ws)
        sess.wait_ready()
        answer1 = run_phase(sess, case.get("acts") or [], case.get("probe"),
                            answer_yes, ctx, "一", minutes)

        answer2 = None
        if case.get("resume"):
            log("EOF 退出第一段进程（优雅收尾）")
            sess.eof()
            log("spawn --resume 第二段进程")
            sess2 = AgentSession(ws, resume=True)
            sess2.wait_ready()
            r = case["resume"]
            answer2 = run_phase(sess2, r.get("acts") or [], r.get("probe"),
                                answer_yes, ctx, "二(resume)", minutes)
            sess2.eof()
        else:
            sess.eof()

        #answer 判定口径：探针幕的完整 stdout 切片（expect_exact 是子串匹配）。
        #不切"助手"之后的正文 —— 那个标记在输出里位置不保证，切片宽一点换来稳定；
        #期望串用独特 token（needle/暗号类），撞上工具行的概率可忽略
        trial["answer"] = answer2 if answer2 is not None else answer1
        trial["events"] = collect_events(ws, started)
        if sess.proc.poll() is None:
            sess.eof()
    except Exception as e:
        trial["error"] = f"{type(e).__name__}: {e}"
        log(f"异常：{trial['error']}")
        trial["events"] = trial.get("events") or collect_events(ws, started)
    finally:
        try:
            if sess is not None:
                raw_log.write_text("".join(sess.chunks), encoding="utf-8")
        except Exception:
            pass
        if mem_bak.exists():
            shutil.rmtree(mem, ignore_errors=True)
            shutil.copytree(mem_bak, mem)
            shutil.rmtree(mem_bak.parent, ignore_errors=True)
    finish()
    sys.exit(0)


if __name__ == "__main__":
    main()
