from dotenv import load_dotenv
import os
from pathlib import Path
import ui

class SkillLoader:
    BASE_DIR = Path(__file__).resolve().parent
    def __init__(self):
        load_dotenv()
           
        self.dir = self.BASE_DIR/os.getenv("SKILL_DIR")
        self.registry = {}

        self.scan()

    #扫描skill
    def scan(self):
        if not self.dir.exists():
            ui.warn("路径错误")
            return

        for sub_dir in self.dir.iterdir():

            if not sub_dir.is_dir():
                continue

            file = sub_dir / "SKILL.md"

            if not file.is_file():
                continue

            name = None
            description = None

            with open(file, "r", encoding="utf-8") as f:

                for line in f:
                    line = line.strip()

                    if line.lower().startswith("name:"):
                        name = line.split(":", 1)[1].strip()

                    elif line.lower().startswith("description:"):
                        description = line.split(":", 1)[1].strip()

                    if name and description:
                        break

            if not name:
                name = sub_dir.name

            self.registry[name] = {
                "name": name,
                "description": description or "",
                "path": file,
            }

        for name in self.registry:
            ui.debug(f"加载skill:{name}")

    #返回skill元数据
    def catalog(self):

        if not self.registry:
            return "没有这个skill"

        return "\n".join(
            f"- {skill['name']}: {skill['description']}"
            for skill in self.registry.values()
        )

    #加载skill内容
    def load(self, name: str):

        skill = self.registry.get(name)

        if skill is None:
            return f"没有这个skill:'{name}'"

        try:
            return skill["path"].read_text(encoding="utf-8")

        except Exception as e:
            return f"warn: '{name}': {e}"