# ClaudeMini

用 Python 手写的迷你代码 Agent。

没用 LangChain 之类的框架，主循环、工具表、上下文压缩、权限钩子都是自己实现的。目的是把 Claude Code 里那些在内部跑、平时看不见的东西，拆成一个个能读懂也能改的 Python 模块。

跑起来就是个命令行对话：你提问，它调工具（bash、读写文件、派子 agent……），把结果回填给模型，循环到任务做完。

## 能力

- **四层上下文压缩**——工具结果限额、微压缩、片段裁剪、模型总结
- **长期记忆**——模型自己判断什么时候该记、什么时候该回忆，不是每轮强制提取
- **子 Agent**——把活分给一个全新的实例独立跑，可以前台等，也可以丢后台
- **后台任务**——长命令和后台 agent 跑在别的线程上；线程要是异常死了、没留下结果，看门狗补一条失败通知，主 agent 能发现
- **定时任务**——cron 五段式登记，到点派进一个独立的后台 agent 执行，不占用你的对话
- **Agent 团队**——常驻成员各占一条线程，成员之间直接走消息总线；下线要双方确认
- **MCP**——用 stdio 连外部进程，把它的工具并进自己的工具表
- **Skill 按需加载**——只把目录写进提示词，需要时再读全文
- **权限钩子**——命令级权限判定，敏感操作弹窗问人

## 跑起来

Windows + Python 3.13，bash 工具依赖 Git Bash。

```
pip install anthropic python-dotenv prompt_toolkit rich
```

把 `.env.example` 复制成 `.env`，填上 `ANTHROPIC_API_KEY` 和 `ANTHROPIC_BASE_URL`（走 Anthropic 兼容接口，换供应商只改这两项），然后：

```
python main.py
```

`.env` 已被 `.gitignore` 挡住，不会进仓库。

## 代码分布

| 文件 | 干什么 |
|---|---|
| `main.py` | 入口：输入循环、Hook 注册、派发器启动 |
| `claude.py` | `ClaudeMini`——agent 主循环、子 agent 与团队编排、工具注册 |
| `llm.py` | 模型通信、历史总结、调用超时、异常翻译 |
| `TOOLS.py` | 24 个工具的 Schema 与处理函数 |
| `compact.py` | 四层上下文压缩 |
| `memory.py` | 长期记忆与经验记忆 |
| `task.py` / `task_runner.py` | 带依赖的任务图 / 后台任务执行器 |
| `corn_job.py` / `dispatcher.py` | 定时任务与派发 |
| `message.py` / `team.py` | 团队消息总线与成员表 |
| `mcp.py` | MCP 客户端 |
| `PROMPT.py` | 系统提示词，按角色切片拼装 |
| `ui.py` | 终端输出与折叠展开 |

纯 Python 部分约 6400 行。

## 文档

- `PROJECT_REPORT.md`——完整实现说明，按子系统讲设计和踩过的坑
- `提示词精简清单.md`——提示词删掉的 40 句各去了哪
- `版本控制.md`——git 用法，以及这个仓库的两条铁律（`.env` 不进库、`autocrlf=false`）

## 说明

这是个实验性质的项目，优先保证机制齐全和可读，没做工程化打磨：出错大量用「返回字符串」而不是抛异常，中文变量名和一些拼写错误（`slient`、`compact_mannager`）都原样留着。

`SKILLS/` 里有两个示例 skill。`mcp_servers.json` 默认只挂一个自带的 demo 服务器。
