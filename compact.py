from dotenv import load_dotenv
import os
from pathlib import Path
import json
import ui

class CompactManager():
    BASE_DIR = Path(__file__).resolve().parent
    def __init__(self):
        load_dotenv()
        self.max_result_limit = int(os.getenv("MAX_RESULT_LIMIT"))
        self.tool_result_path = os.getenv("TOOL_RESULT_PATH")
        self.max_message = int(os.getenv("MAX_MESSAGES"))
        self.transcript = os.getenv("TRANSCRIPT")
        self.context_limit = int(os.getenv("CONTEXT_LIMIT"))

    #保存压缩的信息到文件
    def save_tool_result(self, tool_use_id, content):
        path = self.BASE_DIR / self.tool_result_path
        filepath = path / f"{tool_use_id}.txt"
        try:
            path.mkdir(parents=True, exist_ok=True)          
            if not isinstance(content, str):                  #非 str 先序列化
                content = json.dumps(content, ensure_ascii=False, default=str)
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(content)
            ui.debug(f"完整结果已保存至：{filepath}")
            return str(filepath)
        except Exception as e:
            ui.warn(f"工具结果保存失败：{e}")
            return None
        
    #单次工具调用结果过长
    def tool_result_budget(self, block):
        content = block["content"]
        #已截断过就不再重复处理
        if isinstance(content, str) and "[⚠️ 工具返回结果过长" in content:
            return block
        #统一成成字符串
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False, default=str)
        if len(content) <= self.max_result_limit:
            return block
        path = self.save_tool_result(block["tool_use_id"], content)
        substr = content[:self.max_result_limit]
        block["content"] = (
                f"{substr}\n\n"
                f"[⚠️ 工具返回结果过长，已截断。完整结果已保存至: {path}]\n"
                f"完整结果已归档。如确实需要，请通过适当的读取方式获取相关部分。"
            )
        return block

    #压缩message数量
    def snip_compact(self, messages:list):
        head_end = 3
        tail_start = len(messages)-(self.max_message-head_end-1)

        #配对保护,tool_use和对应的tool_result不能被拆开(仅当确有中间段可裁时才看边界)
        if head_end < tail_start:
            #头部最后一条消息若是tool_use则往后找到它的tool_result一起保留
            if self.has_tool_use(messages[head_end-1]):
                while head_end < tail_start and self.has_tool_result(messages[head_end]):
                    head_end += 1
            #尾部第一条若是tool_result且其tool_use会被归档则回退一位整对保留
            if (0 < tail_start < len(messages)
                    and self.has_tool_result(messages[tail_start])
                    and self.has_tool_use(messages[tail_start-1])):
                tail_start -= 1
            
        if tail_start -1> head_end:
            path = self.BASE_DIR/self.transcript
            middle = messages[head_end:tail_start]
            try:
                payload = json.dumps(
                    middle,
                    ensure_ascii=False,
                    indent=2,
                    default=lambda o: vars(o) if hasattr(o, "__dict__") else str(o)
                )
                with open(path, 'w', encoding='utf-8') as f:
                    f.write(payload)
            except Exception as e:
                ui.warn(f"压缩消息保存失败:{e}")

            #不新增一条消息，将断点信息加到首部最后一条消息中
            note = f"[{tail_start - head_end} messages archived at {self.transcript}]"
            head = messages[:head_end]
            last = head[-1]
            if isinstance(last, dict):
                content = last.get("content")
                if isinstance(content, str):
                    last["content"] = content + "\n" + note
                elif isinstance(content, list):
                    content.append({"type": "text", "text": note})
                else:
                    last["content"] = note
                return head + messages[tail_start:]
            # 头部消息非 dict,退化为不裁剪,避免破坏消息结构
            return messages
        else:
            return messages

    #压缩已处理过的工具调用上下文
    def micro_compact(self,history):
        tool_message = []
        #找出工具调用的消息
        for message in history:
            if message["role"]!="user":
                continue
            if not isinstance(message["content"], list):
                continue
            if any(block.get("type")=="tool_result" for block in message["content"]):
                tool_message.append(message)
        #保留最近三个调用消息
        old_messages = tool_message[0:-3]
        for message in old_messages:
            for block in message["content"]:
                if block.get("type") != "tool_result":
                    continue
                content = block.get("content", "")
                if not isinstance(content, str):                 
                    content = json.dumps(content, ensure_ascii=False, default=str)
                if len(content) <= 120:
                    continue
                #tool_result_buget已存过原始完整内容,直接复用路径
                filepath = self.BASE_DIR / self.tool_result_path / f"{block['tool_use_id']}.txt"
                if filepath.exists():
                    path = str(filepath)
                else:
                    path = self.save_tool_result(block["tool_use_id"], content)
                block["content"] = (
                    "[Earlier tool result compacted]\n"
                    f"Full result: {path}"
                )
        return history

    #归档整段历史
    def save_history(self, history):
        path = self.BASE_DIR / "history_backup.json"
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(
                    json.dumps(
                        history,
                        ensure_ascii=False,
                        default=str,
                        indent=2
                    )
                )
            ui.debug(f"完整历史已保存至：{path}")
            return str(path)
        except Exception as e:
            ui.warn(f"历史保存失败：{e}")
            return None

    #调用大模型总结上下文
    def llm_compact(self, history, llm):
        if (self.estimate_size(history)) > self.context_limit:
            transcript_path = self.save_history(history)
            try:
                summary = llm.summarize_history(history)
            except Exception as e:
                ui.status(f"总结失败:{e},本次跳过压缩")
                return history
            if not summary:
                ui.status("总结为空,本次跳过压缩")
                return history
            new_history = [
                {
                    "role": "user",
                    "content": (
                        "上下文已压缩\n"
                        "以下是之前任务的状态摘要：\n"
                        f"{summary}\n"
                        f"完整历史已保存至：{transcript_path}"
                    )
                }
            ]
            if history and isinstance(history[0], dict) and history[0].get("role") == "system":
                new_history.insert(0, history[0])
            return new_history
        else:
            return history

    #API报prompt过长时的紧急兜底:保留最近keep_recent条消息,旧历史交给模型总结
    def reactive_compact(self, history, llm, keep_recent=5):
        tail_start = max(0, len(history) - keep_recent)
        #配对保护: 尾部第一条是tool_result且其tool_use会被归档 → 回退一位整对保留
        if (0 < tail_start < len(history)
                and self.has_tool_result(history[tail_start])
                and self.has_tool_use(history[tail_start-1])):
            tail_start -= 1
        transcript_path = self.save_history(history)
        old = history[:tail_start] if tail_start else history
        try:
            summary = llm.summarize_history(old)
        except Exception as e:
            ui.status(f"兜底总结失败:{e},放弃重试")
            return history
        if not summary:
            ui.status("兜底总结为空,放弃重试")
            return history
        new_history = [{
            "role": "user",
            "content": (
                "上下文已压缩(紧急兜底)\n"
                "以下是之前任务的状态摘要：\n"
                f"{summary}\n"
                f"完整历史已保存至：{transcript_path}"
            )
        }]
        if history and isinstance(history[0], dict) and history[0].get("role") == "system":
            new_history.insert(0, history[0])
        return new_history + (history[tail_start:] if tail_start else [])

     #计算消息大小
    def estimate_size(self,history):
        text = json.dumps(
            history,
            ensure_ascii = False,
            default = str
        )
        return len(text)

    #判断消息是否含tool_use(block 可能是 dict,也可能是 SDK 块对象)
    def has_tool_use(self,message):
        if isinstance(message,dict):
            content = message.get("content")
            if isinstance(content,list):
                for block in content:
                    btype = block.get("type") if isinstance(block,dict) else getattr(block,"type",None)
                    if btype == "tool_use":
                        return True
        return False

    #判断消息是否含tool_result(block 可能是 dict,也可能是 SDK 块对象)
    def has_tool_result(self,message):
        if isinstance(message,dict):
            content = message.get("content")
            if isinstance(content,list):
                for block in content:
                    btype = block.get("type") if isinstance(block,dict) else getattr(block,"type",None)
                    if btype == "tool_result":
                        return True
        return False                    


        
    
        