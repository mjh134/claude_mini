# -*- coding: utf-8 -*-
"""pytest 的 import 路径注入：单测全部 import src/ 下的模块（from PERMISSIONS import ...），
tests/ 目录本身不是包，靠 conftest 把 src/ 加进 sys.path —— pytest 会自动加载本文件。
直跑模式（python tests/test_xxx.py）不经过 conftest，靠各文件头部的自举两行。"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
