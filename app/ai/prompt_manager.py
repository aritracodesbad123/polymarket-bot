from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parent.parent.parent / "prompts"


class PromptManager:
    def __init__(self, version: str) -> None:
        self.version = version
        path = PROMPTS_DIR / f"{version}.md"
        if not path.exists():
            path = PROMPTS_DIR / "probability_v1.md"
        self.body = path.read_text(encoding="utf-8")

    def render(self, **kwargs: str) -> str:
        text = self.body
        for k, v in kwargs.items():
            text = text.replace("{{" + k + "}}", v)
        return text
