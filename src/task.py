import json
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path

# 任务三态(已创建 / 已认领 / 已完成)
TASK_PENDING = "pending"            # 已创建
TASK_IN_PROGRESS = "in_progress"    # 已认领
TASK_COMPLETED = "completed"        # 已完成


#任务数据类
@dataclass
class Task:
    id: str
    subject: str
    description: str
    status: str = TASK_PENDING                  # 状态:已创建/已认领/已完成
    owner: str | None = None                    # 认领者(claim 时写入)
    blockedBy: list[str] = field(default_factory=list)  #依赖的任务id列表:先完成后才能执行本任务

    # 行为一:被认领。仅已创建(pending)可认领,成功后进入已认领(in_progress)
    def claim(self, owner: str) -> None:
        if self.status != TASK_PENDING:
            raise ValueError(
                f"任务 {self.id} 当前为 {self.status},只有已创建(pending)的任务才能被认领")
        if not owner:
            raise ValueError(f"认领任务 {self.id} 时不能缺少认领者")
        self.status = TASK_IN_PROGRESS
        self.owner = owner

    # 行为二:被完成。仅已认领(in_progress)可完成,且须由认领者本人完成,完成后为终态
    def complete(self, actor: str) -> None:
        if self.status != TASK_IN_PROGRESS:
            raise ValueError(
                f"任务 {self.id} 当前为 {self.status},只有已认领(in_progress)的任务才能被完成")
        if actor != self.owner:
            raise ValueError(f"任务 {self.id} 由 {self.owner} 认领,{actor} 无权完成")
        self.status = TASK_COMPLETED


#任务管理类
class TaskStore:

    def __init__(self, store_path: str):
        self.store_path = Path(store_path)  #任务库文件:启动加载、每次变更写回
        self.tasks = {}          # id -> Task
        self._load()

    #生成任务id:task_ + uuid(全局唯一,免跨会话/跨库撞号)
    def _new_id(self) -> str:
        return f"task_{uuid.uuid4().hex}"

    #启动时从文件加载(跨会话可读;文件不存在则视为空库)
    def _load(self) -> None:
        if not self.store_path.exists():
            return
        raw = json.loads(self.store_path.read_text(encoding="utf-8"))
        self.tasks = {tid: Task(**t) for tid, t in raw.get("tasks", {}).items()}

    #变更即存:把整个任务表写回文件
    def _save(self) -> None:
        payload = {
            "tasks": {tid: asdict(t) for tid, t in self.tasks.items()},
        }
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        self.store_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    #创建任务节点:可带依赖一起建边;新建任务落库并返回其id
    def create_task(self, subject: str, description: str = "",
                    depends_on: list[str] | None = None) -> str:
        if not subject or not subject.strip():
            raise ValueError("创建任务必须提供 subject(任务名)")
        task = Task(id=self._new_id(), subject=subject, description=description)
        for dep_id in depends_on or []:  # 新节点无入边不可能成环,仅需存在性/自依赖/重复守卫
            if dep_id == task.id:
                raise ValueError(f"创建任务失败:任务不能依赖自身")
            if self.get_task(dep_id) is None:
                raise ValueError(f"创建任务失败:依赖任务 {dep_id} 不存在")
            if dep_id not in task.blockedBy:
                task.blockedBy.append(dep_id)
        self.tasks[task.id] = task
        self._save()
        return task.id

    #获取任务节点
    def get_task(self,id:str):
        return self.tasks.get(id)
    
    # 若 dep 已(直接或间接)依赖 id,再让 id 依赖 dep 会成环;从 dep 沿依赖链向上找 id
    def _would_form_cycle(self, id: str, dependency_id: str) -> bool:
        stack = list(self.tasks[dependency_id].blockedBy)
        seen = set()
        while stack:
            cur = stack.pop()
            if cur == id:
                return True
            if cur in seen:
                continue
            seen.add(cur)
            node = self.tasks.get(cur)
            if node:
                stack.extend(node.blockedBy)
        return False

    #创建任务边:让 id 依赖 dependency_id(dependency_id 需先完成,id 才能执行)
    def add_dependency(self, id: str, dependency_id: str) -> None:
        task = self.get_task(id)
        dep = self.get_task(dependency_id)
        if task is None:
            raise ValueError(f"添加依赖失败:任务 {id} 不存在")
        if dep is None:
            raise ValueError(f"添加依赖失败:依赖任务 {dependency_id} 不存在")
        if id == dependency_id:
            raise ValueError(f"添加依赖失败:任务 {id} 不能依赖自身")
        if dependency_id in task.blockedBy:
            return  # 该依赖已存在,幂等跳过
        if self._would_form_cycle(id, dependency_id):
            raise ValueError(f"添加依赖失败:会使任务 {id} 与 {dependency_id} 形成循环依赖")
        task.blockedBy.append(dependency_id)
        self._save()

    #检查依赖状况:返回仍未完成的依赖id清单;空表 = 依赖全部完成,任务可执行
    def check_dependencies(self, task_id: str) -> list[str]:
        task = self.get_task(task_id)
        if task is None:
            raise ValueError(f"检查依赖失败:任务 {task_id} 不存在")
        blocking = []
        for dep_id in task.blockedBy:
            dep = self.get_task(dep_id)
            if dep is None or dep.status != TASK_COMPLETED:
                blocking.append(dep_id)
        return blocking

    #认领任务:依赖未全完成会拒绝;成功则进入已认领(in_progress)并落盘
    def claim_task(self, id: str, owner: str) -> bool:
        task = self.get_task(id)
        if task is None:
            raise ValueError(f"认领失败:任务 {id} 不存在")
        blocking = self.check_dependencies(id)
        if blocking:
            raise ValueError(f"认领失败:任务 {id} 的依赖未完成,阻塞于 {blocking}")
        task.claim(owner)   # 仅 pending 可认领,越界(重复/完成态)由 Task 内部抛错
        self._save()
        return True


    #完成任务:仅已认领(in_progress)且由认领者本人(actor==owner)可完成,完成后落盘
    def complete_task(self, id: str, actor: str) -> bool:
        task = self.get_task(id)
        if task is None:
            raise ValueError(f"完成任务失败:任务 {id} 不存在")
        task.complete(actor)   # 仅 in_progress 可完成、须 actor==owner,越界由 Task 内部抛错
        self._save()
        return True

    #列出所有任务
    def list_tasks(self):
        return list(self.tasks.values())


#===== 工具 handler 工厂 =====
#Schema 声明在 TOOLS.py;这里把 TaskStore 包装成模型可调用的工具。
#依赖(store)由调用方(claude.py)递进来,本模块不 import TOOLS,依赖方向保持单向

def _format_task(store, t):
    """把单个任务渲染成一行可读文本(status/id/subject/owner/描述/阻塞)。"""
    line = f"[{t.status}] {t.id} 「{t.subject}」"
    if t.owner:
        line += f" owner={t.owner}"
    if t.description:
        line += f" | {t.description}"
    if t.blockedBy:
        blocking = store.check_dependencies(t.id)
        line += f" | 依赖:{','.join(t.blockedBy)}"
        if blocking:
            line += "(未完成→阻塞)"
    return line

def create_task_handlers(store):
    """把 TaskStore 的方法包装成模型可调用的工具handler(统一捕获ValueError返回可读文本)。"""
    def task_create(subject, description="", depends_on=None):
        try:
            tid = store.create_task(subject, description, depends_on)
            return f"已创建任务 {tid}:「{subject}」"
        except ValueError as e:
            return str(e)

    def task_list(status=None):
        tasks = store.list_tasks()
        if status:
            tasks = [t for t in tasks if t.status == status]
        if not tasks:
            return "当前没有任务。" + (f"(筛选:{status})" if status else "")
        lines = []
        for st in (TASK_PENDING, TASK_IN_PROGRESS, TASK_COMPLETED):  # 按状态分组,便于规划
            for t in tasks:
                if t.status == st:
                    lines.append(_format_task(store, t))
        return "\n".join(lines)

    def task_get(id):
        t = store.get_task(id)
        if t is None:
            return f"任务 {id} 不存在"
        return _format_task(store, t)

    def task_claim(id, owner="assistant"):
        try:
            store.claim_task(id, owner)
            return f"已认领任务 {id},状态 in_progress(owner={owner})"
        except ValueError as e:
            return str(e)

    def task_complete(id, actor="assistant"):
        try:
            store.complete_task(id, actor)
            return f"已完成任务 {id}"
        except ValueError as e:
            return str(e)

    return {
        "task_create": task_create,
        "task_list": task_list,
        "task_get": task_get,
        "task_claim": task_claim,
        "task_complete": task_complete,
    }
    

