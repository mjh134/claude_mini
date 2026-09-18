import threading
from bash_exec import execute_bash
import time

class BackgroundManager:
    def __init__(self):
        self.tasks = {}
        self.ready = []
        self.results = {}
        self.counter = 0
        self.lock = threading.Lock()

    #启动后台任务
    def start(self,block):
        command = block.input.get("command")
        with self.lock:
            self.counter += 1
            task_id = f"bg_{self.counter:04d}"
            self.tasks[task_id] = {
                "block": block,
                "status": "running"
            }
        thread = threading.Thread(target=self.run, args=(task_id, command),daemon=True)
        thread.start()
        return task_id

    #执行后台任务
    def run(self, task_id, command):
        try:
            output,exit_code = execute_bash(command)
            status = "completed" if exit_code == 0 else "failed"
        except Exception as e:
            output = str(e)
            status = "failed"
        with self.lock:
            self.tasks[task_id]["status"] = status
            self.results[task_id] = output
            self.ready.append(task_id)


    #获取后台任务结果
    def collect(self):
        with self.lock:
            ready = self.ready.copy()
            self.ready.clear()

            results = {}

            for task_id in ready:
                results[task_id] = self.results.pop(task_id)
                self.tasks.pop(task_id)

            return results

    #是否还有后台任务
    def has_background_tasks(self):
        with self.lock:
            return len(self.tasks) > 0

    #有界等待后台任务完成
    def wait_background_tasks(self, timeout=300):
        deadline = time.monotonic() + timeout
        while True:
            with self.lock:
                running = any(task["status"] == "running" for task in self.tasks.values())
            if not running:
                return True
            if time.monotonic() > deadline:
                return False
            time.sleep(0.3)