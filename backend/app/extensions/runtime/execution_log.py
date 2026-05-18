from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable


@dataclass
class ExecutionLog:
    lines: list[str] = field(default_factory=list)
    stage: str | None = None
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc).astimezone())
    emit_line: Callable[[str], None] | None = field(default=None, repr=False)
    emit_stage: Callable[[str], None] | None = field(default=None, repr=False)

    def set_stage(self, stage: str | None) -> None:
        self.stage = stage
        if self.emit_stage and stage:
            self.emit_stage(str(stage))

    def section(self, title: str) -> None:
        title = str(title or "").strip() or "阶段"
        line = f"==============={title}==============="
        self.lines.append(line)
        if self.emit_line:
            self.emit_line(line)

    def line(self, text: str = "") -> None:
        line = str(text)
        self.lines.append(line)
        if self.emit_line:
            self.emit_line(line)

    def render(self) -> str:
        return "\n".join(self.lines).rstrip() + "\n"
