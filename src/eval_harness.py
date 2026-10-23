#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""行为评测编排器（父进程）：读用例 → 每格派子进程 → 收 result → 出报告。

    python eval_harness.py                  # 全部用例各 1 trial
    python eval_harness.py --only comp_sanity_nopressure --trials 1
    python eval_harness.py --list

父进程零业务 import：不碰 claude/llm，判定在子进程里完成。
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import yaml

SRC = Path(__file__).resolve().parent
ROOT = SRC.parent
CASES_DIR = ROOT / "evals" / "cases"
OUT_DIR = ROOT / "out" / "eval"
WS_DIR = ROOT / "out" / "eval_ws"
REPORT_MD = ROOT / "out" / "eval_report.md"
REPORT_JSON = ROOT / "out" / "eval_report.json"
DEFAULT_MINUTES = 6

ENV = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
for key in ("BENCHMARK_TOKEN", "BENCHMARK_BASE_URL"):
    ENV.pop(key, None)          # 离线评测不该带着平台凭证跑

BADGE = {"pass": "✅ PASS", "fail": "❌ FAIL", "not_triggered": "⚪ NOT-TRIGGERED",
         "error": "💥 ERROR"}


def load_cases(only):
    paths = sorted(CASES_DIR.glob("*.yaml"))
    cases = []
    for p in paths:
        case = yaml.safe_load(p.read_text(encoding="utf-8"))
        case["id"] = case.get("id", p.stem)
        case["_path"] = str(p)
        if not only or case["id"] in only:
            cases.append(case)
    return cases


def run_trial(case, trial_no):
    ws = WS_DIR / case["id"]
    trial_dir = OUT_DIR / f"{case['id']}.t{trial_no}"
    shutil.rmtree(ws, ignore_errors=True)
    shutil.rmtree(trial_dir, ignore_errors=True)
    ws.mkdir(parents=True)
    trial_dir.mkdir(parents=True)

    minutes = float((case.get("budget") or {}).get("max_minutes", DEFAULT_MINUTES))
    cmd = [sys.executable, str(SRC / "eval_runner.py"),
           "--case", case["_path"], "--trial-dir", str(trial_dir)]
    started = time.time()
    try:
        proc = subprocess.run(cmd, cwd=str(ws), env=ENV, capture_output=True,
                              text=True, encoding="utf-8", errors="replace",
                              timeout=minutes * 60 + 120)
        tail = "\n".join((proc.stdout + proc.stderr).splitlines()[-8:])
        print(tail, flush=True)
    except subprocess.TimeoutExpired as e:
        log_txt = "\n".join((e.stdout or b"").decode("utf-8", "replace")
                            .splitlines()[-8:]) if e.stdout else "（无输出）"
        print(log_txt, flush=True)
        return {"case": case["id"], "suite": case.get("suite", "capability"),
                "trial_no": trial_no, "verdict": "error", "assertions": [],
                "metrics": {}, "answer": "", "trace_path": "", "ws_path": str(ws),
                "error": f"父进程超时（>{minutes:.0f}min）强杀",
                "duration_s": round(time.time() - started, 1)}

    result_file = trial_dir / "result.json"
    if not result_file.is_file():
        return {"case": case["id"], "suite": case.get("suite", "capability"),
                "trial_no": trial_no, "verdict": "error", "assertions": [],
                "metrics": {}, "answer": "", "trace_path": "", "ws_path": str(ws),
                "error": "子进程没留下 result.json", "duration_s": round(time.time() - started, 1)}
    try:
        return json.loads(result_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return {"case": case["id"], "verdict": "error", "assertions": [],
                "metrics": {}, "error": f"result.json 损坏:{e}"}


def section_md(r):
    lines = [f"### {BADGE.get(r['verdict'], r['verdict'])}  {r['case']}  (t{r.get('trial_no')})"]
    if r.get("error"):
        lines.append(f"- 异常：{r['error']}")
    for a in r.get("assertions") or []:
        mark = "✔" if a["ok"] else "✖"
        lines.append(f"  - {mark} [{a['grader']}] {a['name']} —— {a['detail']}")
    m = r.get("metrics") or {}
    if m:
        lines.append(f"  - 📊 轮数 {m.get('n_turns')} · 工具 {m.get('tool_call_count')} 次 · "
                     f"token in {m.get('tokens_in')} / out {m.get('tokens_out')} · {m.get('wall_time')}s")
    if r.get("answer"):
        lines.append(f"  - 💬 答案摘录：{r['answer'][:120]!r}")
    if r.get("trace_path"):
        lines.append(f"  - ↻ 看现场：python replay.py {r['trace_path']}")
    return "\n".join(lines)


def write_report(results):
    counts = {}
    for r in results:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
    summary = " · ".join(f"{BADGE.get(k, k)} {v}" for k, v in sorted(counts.items()))

    md = [f"# 评测报告 {time.strftime('%Y-%m-%d %H:%M')}", "",
          f"用例 {len(results)} 格：{summary}", ""]
    warned = [r for r in results if r["verdict"] == "not_triggered"]
    if warned:
        md.append("> ⚠ 未测过的格子（压力没触发，不算数据）："
                  + "、".join(r["case"] for r in warned) + "\n")
    for r in sorted(results, key=lambda x: (x["verdict"] != "fail",
                                            x["verdict"] != "not_triggered",
                                            x["case"])):
        md.append(section_md(r))
        md.append("")
    REPORT_MD.write_text("\n".join(md), encoding="utf-8")
    REPORT_JSON.write_text(json.dumps(results, ensure_ascii=False, indent=2),
                           encoding="utf-8")
    print(f"\n报告：{REPORT_MD}\n      {REPORT_JSON}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="", help="只跑这些 id，逗号分隔")
    ap.add_argument("--trials", type=int, default=0, help="覆盖每格 trial 次数")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()
    only = {s.strip() for s in args.only.split(",") if s.strip()}

    cases = load_cases(only)
    if args.list:
        for c in cases:
            f = c.get("factor") or {}
            print(f"{c['id']:<28} {c.get('suite','?'):<12} "
                  f"layer={f.get('layer','?'):<9} pos={f.get('position','?'):<7} dep={f.get('dependency','?')}")
        return 0
    if not cases:
        print(f"evals/cases/ 下没有匹配的用例（only={only or '全部'}）", file=sys.stderr)
        return 2

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    WS_DIR.mkdir(parents=True, exist_ok=True)
    results = []
    for case in cases:
        trials = args.trials or int(case.get("trials", 1))
        for no in range(1, trials + 1):
            print(f"\n━━━ {case['id']} trial {no}/{trials} ━━━", flush=True)
            results.append(run_trial(case, no))
    write_report(results)
    return 0


if __name__ == "__main__":
    sys.exit(main())
