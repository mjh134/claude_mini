from pathlib import Path
from dotenv import load_dotenv
import os
import json
import re
from datetime import datetime
import ui


# 记忆整理(consolidate)配置
CONSOLIDATE_THRESHOLD_DEFAULT = 50  # 整个会话结束时,经验文件数≥该值触发一次 LLM 去重整理
BACKUP_RETAIN_DEFAULT = 5           # 整理前快照最多保留几份(超出删旧)


class MemoryManager:
    BASE_DIR = Path(__file__).resolve().parent

    def __init__(self):
        load_dotenv(override=True)   # 项目 .env 永远赢,与 llm.py 同规矩:防 shell 里已 export 的同名变量遮住配置
        memory_dir = os.getenv("MEMORY_DIR", "memory")
        self.memory_dir = self.BASE_DIR / memory_dir

        self.long_term_dir = self.memory_dir / "long_term"
        self.experience_dir = self.memory_dir / "experience"
        self.temp_dir = self.memory_dir / "temp"
        self.backup_dir = self.memory_dir / "backups"
        self.index_file = self.experience_dir / "index.json"

        self.consolidate_threshold = int(os.getenv("MEMORY_CONSOLIDATE_THRESHOLD") or CONSOLIDATE_THRESHOLD_DEFAULT)
        self.backup_retain = int(os.getenv("MEMORY_BACKUP_RETAIN") or BACKUP_RETAIN_DEFAULT)

        self._ensure_directories()

    def _ensure_directories(self):
        for d in [self.long_term_dir, self.experience_dir, self.temp_dir, self.backup_dir]:
            d.mkdir(parents=True, exist_ok=True)
            
    def write_memory(self, memory: str, title: str = None, tags: list = None, source: str = "manual") -> str:
        now = datetime.now()
        created = now.isoformat(timespec="seconds")
        # id 需保证唯一: 日期 + 时分秒 + 微秒(同一秒内多次写入也不会撞)
        memory_id = "exp-" + now.strftime("%Y-%m-%d") + "-" + now.strftime("%H%M%S%f")

        if not title:
            first_line = memory.strip().split(chr(10))[0]
            title = first_line.lstrip("#").strip() if first_line.startswith("#") else created

        if not tags:
            tags = self._extract_tags_from_content(memory)

        tags_str = ", ".join(tags)
        frontmatter = "---\nid: " + memory_id + "\ntitle: " + title + "\ntags: [" + tags_str + "]\ncreated: " + created + "\nsource: " + source + "\n---\n\n"

        filename = memory_id + ".md"
        filepath = self.experience_dir / filename

        filepath.write_text(frontmatter + memory, encoding="utf-8")
        self._add_to_index(memory_id, filename, title, tags, created)

        ui.status("记忆已保存至：" + str(filepath))
        return str(filepath)

    def _extract_tags_from_content(self, content: str) -> list:
        tags = re.findall(r"#(\w+)", content)
        headings = re.findall(r"^#+\s+(.+)$", content, re.MULTILINE)[:3]
        for heading in headings:
            tags.extend(heading.lower().split()[:2])
        result = list(dict.fromkeys(tags))[:5]
        return result if result else ["untagged"]

    def _add_to_index(self, memory_id: str, filename: str, title: str, tags: list, created: str):
        index = self._load_index()
        index["memories"][memory_id] = {"file": filename, "title": title, "tags": tags, "created": created}
        for tag in tags:
            if tag not in index["tags"]:
                index["tags"][tag] = []
            index["tags"][tag].append(memory_id)
        index["last_updated"] = created
        self.index_file.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")

    def _load_index(self) -> dict:
        if not self.index_file.exists():
            return {
                "version": 1,
                "last_updated": None,
                "tags": {},
                "memories": {}
            }

        content = self.index_file.read_text(encoding="utf-8")

        if not content.strip():
            return {
                "version": 1,
                "last_updated": None,
                "tags": {},
                "memories": {}
            }

        return json.loads(content)

    def _parse_memory_file(self, content: str):
        if not content.startswith("---"):
            return None, None, content
        parts = content.split("---", 2)
        if len(parts) < 3:
            return None, None, content
        frontmatter = parts[1]
        body = parts[2].strip()
        meta = {}
        for line in frontmatter.split(chr(10)):
            if ":" in line:
                key, value = line.split(":", 1)
                key = key.strip()
                value = value.strip().strip("[]\"' ")
                if key in ("id", "title", "tags", "created", "updated", "source"):
                    if key == "tags":  # tags 解析成 list,避免 rebuild_index 逐字符拆散
                        meta[key] = [t.strip() for t in value.split(",") if t.strip()]
                    else:
                        meta[key] = value
        return meta.get("id"), meta, body

    def load_session_memory(self) -> str:
        memories = []
        for filename in ["user.md", "soul.md", "project.md"]:
            filepath = self.long_term_dir / filename
            if filepath.exists():
                content = filepath.read_text(encoding="utf-8")
                memories.append("## " + filename[:-3] + " 记忆" + chr(10) + chr(10) + content)
        if memories:
            return chr(10) + chr(10) + "---" + chr(10) + chr(10).join(memories)
        return ""

    def get_all_tags(self) -> list:
        return list(self._load_index()["tags"].keys())

    def search_by_tags(self, tags: list) -> list:
        index = self._load_index()
        results = []
        for tag in tags:
            for mem_id in index["tags"].get(tag, []):
                if mem_id in index["memories"]:
                    mem_info = index["memories"][mem_id]
                    filepath = self.experience_dir / mem_info["file"]
                    if filepath.exists():
                        results.append({"id": mem_id, "title": mem_info["title"], "tags": mem_info["tags"], "content": filepath.read_text(encoding="utf-8")})
        return results

    def _extract_json(self, text: str) -> list:
        """鲁棒地解析模型返回的 JSON(容忍代码块围栏 / 多余文字),返回数组。"""
        if not text:
            return []
        t = re.sub(r"^```[a-zA-Z]*\s*", "", text.strip())
        t = re.sub(r"\s*```$", "", t).strip()
        cand = None
        start, end = t.find("["), t.rfind("]")
        if start != -1 and end > start:
            cand = t[start:end + 1]
        else:
            start, end = t.find("{"), t.rfind("}")
            if start != -1 and end > start:
                cand = t[start:end + 1]
        if cand is None:
            return []
        try:
            data = json.loads(cand)
        except Exception as e:
            ui.debug(f"consolidate JSON 解析失败:{e}")
            return []
        if isinstance(data, list):
            return data
        return [data] if isinstance(data, dict) else []

    def rebuild_index(self):
        index = {
            "version": 1,
            "last_updated": datetime.now().isoformat(timespec="seconds"),
            "tags": {},
            "memories": {}
        }

        for filepath in self.experience_dir.glob("*.md"):
            try:
                content = filepath.read_text(encoding="utf-8")
                memory_id, meta, _ = self._parse_memory_file(content)

                if not memory_id:
                    continue

                tags = meta.get("tags", [])

                index["memories"][memory_id] = {
                    "file": filepath.name,
                    "title": meta.get("title", ""),
                    "tags": tags,
                    "created": meta.get("created", "")
                }

                for tag in tags:
                    index["tags"].setdefault(tag, []).append(memory_id)

            except Exception as e:
                ui.debug(f"索引重建时跳过 {filepath.name}: {e}")

        self.index_file.write_text(
            json.dumps(index, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )

        return index

    # ---------- 记忆整理 / 去重(consolidate)----------
    def _count_experience_files(self) -> int:
        return len(list(self.experience_dir.glob("*.md")))

    def _snapshot_experience(self):
        """整理前把整个 experience 目录(含 index.json)快照到 memory/backups/snapshot_<ts>。

        只保留最近 self.backup_retain 份快照,超出自动删旧。
        """
        import shutil
        base = datetime.now().strftime("%Y%m%d_%H%M%S%f")
        dest = self.backup_dir / ("snapshot_" + base)
        i = 1
        while dest.exists():   # 同秒内多次整理也保证目录唯一
            dest = self.backup_dir / ("snapshot_" + base + "_" + str(i))
            i += 1
        shutil.copytree(self.experience_dir, dest)

        snaps = sorted([p for p in self.backup_dir.glob("snapshot_*") if p.is_dir()])
        if self.backup_retain > 0 and len(snaps) > self.backup_retain:
            for old in snaps[:-self.backup_retain]:
                shutil.rmtree(old, ignore_errors=True)
        return dest


    def _collect_memory_digests(self, max_body: int = 800) -> list:
        """把 experience 下每份记忆读成摘要,供 LLM 决策。tags 在 _parse_memory_file 已拆为 list。"""
        digests = []
        for fp in sorted(self.experience_dir.glob("*.md")):
            try:
                content = fp.read_text(encoding="utf-8")
            except Exception:
                continue
            memory_id, meta, body = self._parse_memory_file(content)
            if not meta:
                meta, body = {}, content
            body = (body or "").strip()
            truncated = len(body) > max_body
            digests.append({
                "file": fp.name,
                "title": meta.get("title", "") or fp.stem,
                "tags": meta.get("tags", []) or [],
                "created": meta.get("created", ""),
                "body": body[:max_body] + ("\n…[正文被截断,仅供参考]" if truncated else ""),
            })
        return digests

    def _build_consolidate_prompt(self, digests: list) -> str:
        """构造喂给模型的整理指令:附上全部记忆文件的摘要。"""
        lines = []
        for i, d in enumerate(digests, 1):
            tag_s = ", ".join(d["tags"]) if d["tags"] else "-"
            lines.append(
                f"{i}. file={d['file']} | title={d['title']} | tags=[{tag_s}] | "
                f"created={d['created'] or '-'}\n{d['body']}"
            )
        return (
            "你是一名记忆整理师。以下是长期经验记忆库中全部 "
            + str(len(digests))
            + " 份记忆文件(正文过长会被截断,仅供参考)。\n"
            "请找出:\n"
            "1) 内容完全重复或高度雷同的记忆,只保留信息最全的一份;\n"
            "2) 已过时、错误或失去价值、应删除的记忆;\n"
            "3) 多条碎片化记忆,可合并整理成一份更完整的记忆。\n\n"
            "规则:\n"
            "- 只能引用上面列出的 file 文件名,不得虚构或猜测不存在的内容。\n"
            "- 正文带\"被截断\"标记不代表该文件可删,请勿仅因截断就删除。\n"
            "- 不确定时宁可不处理,不要删除仍具唯一价值的信息。\n"
            "- 只输出一个 JSON 数组,元素为动作对象:\n"
            '  {"op": "delete", "file": "<文件名>", "reason": "..."}\n'
            '  {"op": "merge", "target": "<保留的文件名>", "sources": ["<源1>", "<源2>"], '
            '"title": "<可选:合并后新标题>", "tags": ["<可选:新标签>"], "reason": "..."}\n'
            "- 若无任何需要整理的内容,直接输出 []。\n"
            "除 JSON 数组本身外,不要输出任何其它文字或代码块标记。\n\n"
            "--- 记忆文件列表开始 ---\n"
            + "\n\n".join(lines)
            + "\n--- 记忆文件列表结束 ---"
        )

    def _apply_merge(self, target: str, sources: list, title, tags, updated_ts: str):
        """把 sources 的正文并入 target(frontmatter 保留 target 的 id/created),随后删除 sources。"""
        tp = self.experience_dir / target
        raw = tp.read_text(encoding="utf-8")
        _, meta, tbody = self._parse_memory_file(raw)
        meta = meta or {}
        merged = [(tbody or "").strip()]
        for src in sources:
            sp = self.experience_dir / src
            try:
                sraw = sp.read_text(encoding="utf-8")
            except Exception:
                sraw = ""
            _, smeta, sbody = self._parse_memory_file(sraw)
            sbody = (sbody or "").strip()
            if sbody:
                seg = f"## {smeta.get('title', src)}\n\n{sbody}" if smeta else sbody
                merged.append(seg)
        new_title = title or meta.get("title") or target.rsplit(".md", 1)[0]
        new_tags = tags if isinstance(tags, list) and tags else (meta.get("tags") or [])
        parts = [
            "---",
            "id: " + (meta.get("id") or target.rsplit(".md", 1)[0]),
            "title: " + new_title,
            "tags: [" + ", ".join(new_tags) + "]",
            "created: " + (meta.get("created") or updated_ts),
            "source: " + (meta.get("source") or "manual"),
            "updated: " + updated_ts,
            "---",
            "",
            "\n\n---\n\n".join([p for p in merged if p]),
            "",
        ]
        tp.write_text("\n".join(parts), encoding="utf-8")
        for src in sources:
            (self.experience_dir / src).unlink(missing_ok=True)

    def consolidate(self, llm=None) -> dict:
        """把当前经验记忆库发给模型做去重/整理(LLM 驱动)。

        流程: 收集文件摘要 -> 模型给动作(delete/merge) -> 校验动作 -> 先快照 -> 再应用 -> rebuild_index。
        安全约束: 确有动作时才先快照,任何一步出错时原始文件都可在快照里回滚;
        单个动作非法/异常只记入 skipped,不阻断整体。llm 为 None 或目录为空时跳过。
        返回值用于记录整理日志。
        """
        ts = datetime.now().isoformat(timespec="seconds")
        n_before = self._count_experience_files()
        report = {"ts": ts, "before": n_before, "after": n_before,
                  "deleted_files": [], "merged_files": [], "snapshot": None,
                  "skipped": [], "note": None}
        files = sorted(self.experience_dir.glob("*.md"))
        if llm is None or not files:
            report["note"] = "跳过:未提供 llm 或暂无记忆文件"
            return report

        digests = self._collect_memory_digests()
        try:
            response = llm.client.messages.create(
                model=llm.model_id,
                max_tokens=2048,
                messages=[{"role": "user", "content": self._build_consolidate_prompt(digests)}],
            )
        except Exception as e:
            report["note"] = f"consolidate 调用失败,本次不整理: {e}"
            return report

        text = "".join(b.text for b in response.content if b.type == "text")
        actions = self._extract_json(text)
        actions = [a for a in actions if isinstance(a, dict)]

        names = {fp.name for fp in files}
        used = set()
        merges, deletes = [], []
        for a in actions:
            op = a.get("op")
            if op == "delete":
                f = a.get("file")
                if isinstance(f, str) and f in names and f not in used:
                    used.add(f)
                    deletes.append(f)
                else:
                    report["skipped"].append(f"delete {f}(文件不存在/重复指定)")
            elif op == "merge":
                target = a.get("target")
                srcs = [x for x in (a.get("sources") or [])
                        if isinstance(x, str) and x in names and x != target and x not in used]
                if isinstance(target, str) and target in names and target not in used and srcs:
                    used.add(target)
                    for x in srcs:
                        used.add(x)
                    merges.append({"target": target, "sources": srcs,
                                   "title": a.get("title"), "tags": a.get("tags")})
                else:
                    report["skipped"].append(f"merge {target} <- {a.get('sources')}(目标/源不存在或冲突)")
            else:
                report["skipped"].append(f"未知 op: {op}")

        if not merges and not deletes:
            # 返回了空数组=模型明确判定无需整理;什么都没返回才是异常
            report["note"] = "模型判定无需整理" if (actions or text.strip()) else "模型未返回任何有效动作"
            return report

        report["snapshot"] = str(self._snapshot_experience())
        for m in merges:
            try:
                self._apply_merge(m["target"], m["sources"], m["title"], m["tags"], ts)
                report["merged_files"].append(m["target"])
                report["deleted_files"].extend(m["sources"])
            except Exception as e:
                report["skipped"].append(f"merge {m['target']} 失败: {e}")
        for f in deletes:
            try:
                (self.experience_dir / f).unlink(missing_ok=True)
                report["deleted_files"].append(f)
            except Exception as e:
                report["skipped"].append(f"delete {f} 失败: {e}")

        self.rebuild_index()
        report["after"] = self._count_experience_files()
        return report

    def consolidate_if_due(self, llm=None) -> dict:
        """整个会话结束时调用:经验文件数达到阈值才触发一次 LLM 整理。异常不外抛,不阻断退出。"""
        empty = {"triggered": False, "ts": datetime.now().isoformat(timespec="seconds"),
                 "before": self._count_experience_files(),
                 "after": self._count_experience_files(), "note": ""}
        try:
            n = self._count_experience_files()
            if n < self.consolidate_threshold:
                empty["note"] = f"未达阈值({n}<{self.consolidate_threshold}),跳过整理"
                return empty
            rep = self.consolidate(llm)
            rep["triggered"] = True
            if rep.get("deleted_files"):
                ui.debug(f"[consolidate] 会话结束整理:删除 {len(rep['deleted_files'])} 份 -> "
                      f"{rep['after']} 份;快照={rep['snapshot']}")
            else:
                ui.debug(f"[consolidate] 会话结束整理:无需删除({rep.get('note') or '无动作'})")
            return rep
        except Exception as e:
            ui.warn(f"[consolidate] 会话结束整理失败(不阻断退出): {e}")
            empty["note"] = f"整理失败: {e}"
            return empty

    def create_handlers(self) -> dict:
        def write_memory(title: str, content: str, tags: list = None) -> str:
            path = self.write_memory(content, title=title, tags=tags or [], source="manual")
            return "记忆已保存至: " + path
        return {"write_memory": write_memory}
