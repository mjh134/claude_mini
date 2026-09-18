#-------------解决agent之间的通信问题--------------


import threading
from dataclasses import dataclass


@dataclass
class Message:
    sender: str         # 发送者 agent 的 ID(如 "alice-7f3a";主agent是 "main")
    receiver: str       # 接收者 agent 的 ID —— 用 ID 寻址,名字可能重复不能当身份
    content: str        # 消息内容(普通消息是自然语言;控制消息是协议说明)
    kind: str = "chat"  # 消息类型:"chat" 普通业务消息;其余是生命周期控制消息(代码处理,不进LLM)
                        # 哪些 kind 算控制消息由调用方决定(见 team.py),这里只负责带这个字段


#存储消息，转发消息
class MessageBus:
    def __init__(self):
        self.messages = []                  # 待领取消息
        self._cond = threading.Condition()  # 锁，实现阻塞与唤醒

    #发送消息，唤醒所有agent检查消息
    def send(self, message: Message) -> bool:
        with self._cond:
            self.messages.append(message)
            self._cond.notify_all()     # 唤醒所有agent检查消息
        #消息流向可见(交互测试用;嫌吵可以删掉这行)
        print(f"[bus] {message.sender} → {message.receiver}: {message.content[:60]}")
        return True

    #领取消息
    def receive(self, receiver: str) -> Message:

        with self._cond:
            while True:#唤醒后重新检查
                for i, msg in enumerate(self.messages):
                    if msg.receiver == receiver:
                        return self.messages.pop(i)    # 取出消息


                self._cond.wait()#没拿到消息继续阻塞并释放锁

    #非阻塞收件:取走所有发给 receiver 的消息,没有就返回空列表
    #给不能阻塞等待的 agent 用(如由用户输入驱动的主agent,它没法挂起等消息)
    #predicate:可选的类型过滤(如只要普通消息、或只要控制消息),由调用方传入 ——
    #总线本身不认识消息类型,只按 predicate 筛,保持"纯基础设施"的职责(设计.md 五)
    def drain(self, receiver: str, predicate=None) -> list:
        with self._cond:
            mine, keep = [], []
            for m in self.messages:
                if m.receiver == receiver and (predicate is None or predicate(m)):
                    mine.append(m)
                else:
                    keep.append(m)
            if not mine:
                return []
            self.messages[:] = keep     #按对象本身取走,不用值比较(避免内容相同的消息被误删)
            return mine

    #只看不取:有没有发给 receiver 的消息
    #给"既要等键盘、又想被消息唤醒"的主循环当探针用(drain 会把消息取走,探针不能取)
    def has_message(self, receiver: str, predicate=None) -> bool:
        with self._cond:
            return any(m.receiver == receiver and (predicate is None or predicate(m))
                       for m in self.messages)

    #只看不取:发给 receiver 的消息条数(成员判断"还有没有活没干"用)
    def count(self, receiver: str, predicate=None) -> int:
        with self._cond:
            return sum(1 for m in self.messages
                       if m.receiver == receiver and (predicate is None or predicate(m)))

