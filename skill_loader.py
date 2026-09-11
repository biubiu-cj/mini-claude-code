from pathlib import Path

from config import BASE_DIR


#skill加载
class SkillLoader:
    """扫描 skills/ 目录下的 SKILL.md,把名称、描述和全文读进内存。"""

    def __init__(self, skills_dir: Path):
        """记住技能目录,技能表先留空。"""
        self.skills_dir = skills_dir
        self.skills = {}

    def scan(self):
        """重新扫描技能目录,重建技能表;目录不存在就清空。"""
        self.skills.clear()

        if not self.skills_dir.exists():
            return

        skills_root = self.skills_dir.resolve()

        for manifest in sorted(self.skills_dir.glob("*/SKILL.md")):
            if not manifest.is_file() or not manifest.resolve().is_relative_to(skills_root):
                continue

            content = manifest.read_text(encoding="utf-8")
            metadata, body = self.parse_frontmatter(content)
            raw_name = metadata.get("name")
            name = raw_name.strip() if isinstance(raw_name, str) else ""
            name = name or manifest.parent.name

            raw_description = metadata.get("description")
            description = raw_description.strip() if isinstance(raw_description, str) else ""
            description = description or body.split("\n", 1)[0]
            description = " ".join(str(description).lstrip("# ").split())

            self.skills[name] = {
                "name": name,
                "description": description,
                "content": content,
            }

    def parse_frontmatter(self, content: str):
        """把 SKILL.md 拆成 (Frontmatter 元数据, 正文);没有 Frontmatter 就返回空表。"""
        if not content.startswith("---"):
            return {}, content

        lines = content.splitlines()

        if len(lines) < 3 or lines[0].strip() != "---":
            return {}, content

        end_index = None

        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                end_index = i
                break

        if end_index is None:
            return {}, content

        metadata = {}

        for line in lines[1:end_index]:
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            key = key.strip()
            value = value.strip()
            metadata[key] = value

        body = "\n".join(lines[end_index + 1:])

        return metadata, body

    def catalog(self) -> str:
        """返回给模型看的技能目录(只有名字和描述)。"""
        if not self.skills:
            return "none"

        lines = []

        for skill in self.skills.values():
            lines.append(f"- {skill['name']}: {skill['description']}")

        return "\n".join(lines)

    def load(self, name: str) -> str:
        """按名字返回技能全文,不存在则返回带可用列表的错误文本。"""
        skill = self.skills.get(name)

        if skill:
            return skill["content"]

        available = ", ".join(self.skills) or "none"

        return f"Error: Unknown skill '{name}'. Available: {available}"


SKILL_LOADER = SkillLoader(BASE_DIR / "skills")
SKILL_LOADER.scan()
