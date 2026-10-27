# evals/ — 评测集（按能力分目录）

测什么、为什么、怎么判，需求锚点见 `doc/ROADMAP.md` 项 3/4 需求澄清。跑法：

```
python src/eval_harness.py                   # 全部用例，递归扫 cases/ 各主题目录
python src/eval_harness.py --group schema    # 只跑某个能力目录（compact / schema …）
python src/eval_harness.py --only comp_sanity_nopressure
python src/eval_harness.py --list            # 按主题分组看因子坐标
```

## 目录布局

用例按**被测能力**分目录存放，`eval_harness.py` 递归扫 `cases/` 下所有 `*.yaml`（新增主题＝建目录，不用改代码）：

```
evals/cases/
├── compact/   # 上下文压缩因子集 v0（12 格，见下）
└── schema/    # 工具入参校验（1 格，见下）
```

复测按能力目录整组跑：`--group <目录名>`（如 `--group schema` 只跑入参校验那组）；`--list` 按目录分组列出全部格子与因子坐标。

## schema/：工具入参校验

| 格 | 判据 | 首次实测 |
|---|---|---|
| tool_schema_block_recover | 终态 target.txt=OK20DONE（判 outcome）；拦截是否真触发看 trace | ✅ 2026-10-24，trials=2 全 PASS——模型自发把 write_file 的 content 发成整数 20，被 `check_schema` 硬拦、零副作用并自愈 |

复测：`python src/eval_harness.py --group schema --trials 2`。设计见 `doc/decisions/0004`。

## bgtask/：后台任务管理（ROADMAP 项 7）

被测能力 = 后台任务的提交落盘 / 完成通知 / 中断恢复 / 坏日志降级。runner 按用例顶层 `bg_log` 声明注入日志文件（只有这组格带落盘，其余格零污染）；fixture 可用 `raw: true` 全文预置遗留日志，模拟“上次进程被强杀”的世界。

| 格 | 因子 | 判据 |
|---|---|---|
| bgtask_bash_done | bash×完成 | 探针报出产出标记；产物文件在；**盘上条目收尾即删**（content_absent） |
| bgtask_disk_log | bash×落盘 | 盘上硬证据：未收尾任务的日志含原命令（提交即落盘）；模型能报出命令原文 |
| bgtask_crash_recover | bash×恢复 | 预置遗留日志 → 首轮通知 → 探针报出原命令片段+说清中断；**产物必须不存在**（must_absent，抓“自作主张重放”）；盘已清空（只交代一次） |
| bgtask_corrupt_log | bash×坏日志 | 坏 JSON 不炸启动；.bad 留证；正常工作照常完成 |

复测：`python src/eval_harness.py --group bgtask --trials 1`

为此组新增的基建：`state_check.files` 支持 `content_absent`（存在但不得含）/ `must_absent`（必须不存在，替代存在性判定）；`setup.files` 支持 `raw: true`（全文直写）。**首轮踩坑**：literal 是“往填充文本里插一行”的语义，格式敏感的 JSON fixture 混进填充字符变坏 JSON，中断交代根本没发生、被模型翻源码当场解剖（48 轮）——fixture 必须用 raw。

**首轮实测（2026-10-24，trials=1）：4/4 PASS**。crash_recover 两轮调优记档，都是评测体系在抓真问题：①首轮 FAIL——fixture 踩 literal 语义坑（混入填充文本变坏 JSON，中断交代根本没发生，被模型翻源码当场解剖，48 轮）→ 修出 `raw` 直写；②二轮 FAIL——机制层六条断言全过、6 轮完成，唯独字面匹配“中断”二字太脆（模型说“断掉/进程没了”语义全对被挂）→ 删字面判据，“通知送达且被利用”由命令原文回取证明（盘已清空，原文只可能来自通知），“以为还在跑”的真反例答不出原文、被 probe_answer 自然抓住。

## 因子矩阵（compact/）

被测能力 = 上下文压缩。变量五层 × 位置 × 依赖强度，格子 = 一次组合。
阈值按 `.env` 实测校准：`MAX_RESULT_LIMIT=2000`（budget）/ `MAX_MESSAGES=50`（snip）/ `CONTEXT_LIMIT=200000`（llm）。

| # | 格 | layer | pos | dep | 压力手法 | 探针判据 |
|---|---|---|---|---|---|---|
| 1 | comp_sanity_nopressure | none | tail | once | 无（校准格） | 回取，全过才算环境健康 |
| 2 | comp_budget_tail_once | budget | tail | once | 单文件 6k，needle 在 0.8 处 | 回取 + 重读证据 |
| 3 | comp_budget_head_control | budget | head | once | 同上，needle 在 0.08（幸存区） | 回取（对照：这格都挂=截断比预期狠） |
| 4 | comp_budget_tail_reread | budget | tail | repeat | 同 2 | 同 2 + 同参重读 ≤2（重取代价） |
| 5 | comp_micro_mid_once | micro* | middle | once | 6 文件×2500 连读，needle 在 #2（最早被缩成指针） | 回取 + 重读证据（pressure 断言用 budget 作代理信号，见下注） |
| 6 | comp_snip_mid_once | snip | middle | once | 24 文件连读（>50 条 history），needle 在 #8（被归档中段） | 回取 + 重读证据 |
| 7 | comp_snip_mid_sequence | snip | middle | sequence | 同 6，第二探针在 #20 | 双回取组合（漏一即挂） |
| 8 | comp_snip_mid_update | snip | middle | update | 同 6，幕间改写 needle 文件 | 回取新值 + 改写后的重读证据（拿上下文旧值回答=挂） |
| 9 | comp_llm_mid_exact | llm | middle | once | 8 读 + 16 份 14k 拷贝写（顶过 200k） | 回取（摘要换血后） |
| 10 | comp_llm_head_constraint | llm | head | exact | 同 9 | 首词必须是 LOCK<needle>（头部约束过摘要） |
| 11 | comp_llm_mid_list | llm | middle | sequence | 10 读 + 12 份拷贝 | 清单回取 |
| 12 | comp_reactive_mid_once | reactive | middle | once | 42 份拷贝顶到上下文上限，逼 prompt 过长 | 回取（兜底压缩后）。已知高危：总结本身可能超长→error，如实记录 |

**空格（下一批候选）**：llm×tail、micro×sequence、budget×update、五层两两叠加（先 budget 后 snip 的复合压力）、中文内容压力（当前全是 ASCII 填充）。

\* **micro 观测盲区**：micro 的 span 记 before/after=len(history)，原地替换内容条数永不变——用 trace 判「micro 触发了」结构性不可能。该格 pressure 断言用 budget 作代理（文件 2500 字逼 budget 触发，needle 在 0.4<2000 不被截），micro 本身的效果由探针回取测。要真观测需给写侧 micro span 加 replaced 计数（动 claude.py 一行，待人批）。

## e2e/：真实 CLI 入口端到端（交互式运行器）

被测能力 = **`python src/main.py` 这条真实入口本身**：输入循环、权限弹窗（人工应答）、
session 存档、`--resume`。与上面各组的分工：

| 执行器 | 驱动方式 | 权限 ASK 处置 | 测得到 |
|---|---|---|---|
| `eval_runner.py`（其余组） | 进程内 import ClaudeMini | 一律拒绝（无人值守） | 库层能力 |
| `eval_interactive_runner.py`（e2e 组，用例带 `runner: interactive`） | spawn 真实 main.py 子进程，管道逐幕驱动 | **弹窗自动应答**（`permissions.default: y/n`） | CLI 端到端 |

用例 YAML 与库层格同格式（acts/probe/graders 判定器共用），交互专属字段：`runner: interactive`、
`permissions.default`、`resume:`（第一段 EOF 退出后 spawn `--resume` 跑第二段，探针取第二段）。
轮次同步信号 = `<工作区>/.task/session.json` 落盘变化（run_turn 收尾必写）——stdout 的
`User:` 提示符是队列式输入信号，agent 干活时读线程也会打它，不能当轮次边界（首轮实测踩过）。

| 格 | 因子 | 判据 |
|---|---|---|
| e2e_cli_write_run | 入口×基本链路 | 写脚本+运行，盘上文件与探针都含独特标记 E2E-OK-<needle> |
| e2e_permission_allow | 弹窗×应允 | cp 触发弹窗应 y → copy_ask.txt 落盘且内容与 seed 一致 |
| e2e_permission_deny | 弹窗×拒绝 | 应 n → copy_deny.txt **must_absent**（拒了还落盘=穿透）；同参 cp ≤1 次（不重试） |
| e2e_session_resume | 入口×恢复 | 第一段记暗号 → EOF → --resume 新进程探针答出暗号 |

**首轮实测（2026-10-26，trials=1）：4/4 PASS**。顺手抓到两条真问题：

1. **agent bug（✅ 已修 2026-10-27，配 25s 表）**：管道模式（非 tty）下弹窗 fallback `input()`
   （主线程）与读线程 `input("User: ")` 抢同一 stdin——应答被读线程吃掉后，主线程原本
   **无超时永久卡死**（`ask_sync` 的 25s 超时只在 prompt_toolkit 路径生效）。修法：
   fallback 分支改走 `ui._input_with_timeout`（daemon 线程 + `ev.wait(timeout)`，超时按
   拒绝），`src/test_ask_timeout.py` 三场景守回归（answered/timeout/eof，其中"开着不喂"
   就是冻死的最小复刻）。运行器里的补喂补偿保留为深防御（8s < 25s，表来不及触发）。
   已知语义变化：弹窗期间 Ctrl+C 从"拒绝这条继续跑"变成"中断整轮"（KBI 直接上穿），
   与 user_loop 的中断哲学一致。
2. **fixture 坑（已修）**：`setup.files` 的 `literal` 是"往填充文本插一行"语义，
   **必须配 `at`**，否则插不进去（内容判据静默失效）——格式敏感 fixture 一律 `raw: true`，
   与 bgtask 组的教训同源。

另外运行器每次跑前备份、跑后恢复 `PROJECT_ROOT/memory`（agent 写记忆走源码锚定，
不随工作区 cwd），评测对真实记忆零污染。

## 判据（三层，只判 L1/L2）

- `state_check`：探针回取（子串精确匹配预埋值）、环境文件事实。
- `tool_calls`：必需/禁用集（不判顺序）；`no_fabrication`——答对即可，答错时 trace 里有重读=恢复、没有=编造；`max_same_call`——同参重复上限。
- `budget`：轮数 / token 上限，超了就挂。
- 幕间断言 `expect_pressure`：目标压缩层必须真的触发且发生缩减，**未触发判 not_triggered，不算数据**（报告置顶警告）。

## 格子怎么长（三条管道）

1. **真实失败挖掘**：平时用 agent 撞到意外行为 → `replay.py` 定位 → 转成新格。
2. **不变量反推**：ARCHITECTURE 职责表逐条问"什么条件会打破它"。
3. **组合生成**：`out/gen_cases_v0.py` 就是本批生成器，扩格先扩它（阈值改动只动一处）。

## 规矩

- **能力集 KPI：首轮 pass@1 落在 20–60%。全过 = 用例无效**，加难度或退休。
- 格子反复爬满后毕业进回归集（本目录目前全是能力集，回归集待第一批爬坡后分）。
- 每格必须有 reference 解法（写在 YAML 头部注释），评审时人按它走一遍必须能过。
- 防作弊：探针值只存在于 agent 必须付出代价才能拿到的地方（生成器保证），不许写进提示词。
- 成本：轻格 2-5 分钟，snip 格 8-15 分钟，llm 格 15-25 分钟，reactive 格最长；`budget.max_minutes` 是每格墙钟上限，超时=error 不算 fail。

## v0 首轮实测（2026-10-23，deepseek-flash，每格 1 trial）

**结果：10 PASS / 2 FAIL，pass@1 = 83%**（高于 20-60% 目标区间 → 按 KPI 本集偏易，v1 要加难度）。

| 格 | 结果 | 备注 |
|---|---|---|
| sanity / budget×3 / micro / snip_once / snip_update / snip_sequence / llm_head / llm_list | PASS | budget 两格：截断后均用 bash 重读恢复（正是想要的信号）；update 格：改写后重读取新值，没拿上下文旧值 |
| snip_sequence | 首轮 FAIL→修复 fixture 后 PASS | 首轮失败是 fixture 没埋 literal（判定器正确地挂了它）——「判定器也会抓到自己的 bug」的实例 |
| llm_mid_exact | FAIL | 压力触发成功，但模型把 SECRET_TOKEN 当真凭据拒绝回显——**探针命名触发安全拒绝**，v1 改中性名（RECALL_CODE） |
| reactive_mid_once | FAIL（另一轮 PASS，pass@1≈50%） | 真实能力短板：兜底压缩后遗忘且不重读。唯一落在目标区间的格 |

**首轮揪出的 harness/fixture 教训（都已修）**：占位符未渲染、文件名不一致、llm 格被 bash 批量复制绕压（提示词明令 write_file-only）、snip 格被并行读取绕过（改强制逐个+30 文件）、逐幕压力早退会跳过造压幕（检查移到全部幕之后）、micro 层 before==after 结构性不可观测（改 budget 代理断言）。
