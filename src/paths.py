"""项目根锚点:所有"数据/资源放哪"的路径统一从 PROJECT_ROOT 解析。

为什么存在:memory.py / skill.py / compact.py / PERMISSIONS.py 原来各自拿
Path(__file__).parent 拼数据目录,代码一搬家(进 src/),memory/、SKILLS/ 就必须
跟着搬,否则静默失联。收敛到一个常量后,代码归 src/,数据归项目根,两边搬家互不牵连。

覆盖范围只限 __file__ 锚定的那类路径。.task/、llm_request.json、ui_ask.log 这些
**cwd 相对路径**不在此列 —— 它们跟进程工作目录走,靠"从项目根启动"的纪律保证。
"""
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
