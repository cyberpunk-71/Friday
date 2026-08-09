"""SKILLS — genome skill registry (Donna's 73 skills, converted).

FRIDAY-Δ: a SKILL.md is a memory with kind=procedure — retrieved into exactly
ONE slot, so the model reads the one recipe that matters, not 150 titles.
Skills are git-tracked in the genome; a skill ships only with verify() +
VCR cassettes; a skill that fails its cassette is quarantined.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from .config import cfg

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.S)


class Skill:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.name = path.parent.name
        self.category = path.parent.parent.name
        text = path.read_text(encoding="utf-8", errors="ignore")
        m = FRONTMATTER_RE.match(text)
        self.meta: dict = {}
        if m:
            try:
                import yaml
                self.meta = yaml.safe_load(m.group(1)) or {}
            except Exception:
                pass
            self.body = text[m.end():]
        else:
            self.body = text
        self.description = self.meta.get("description", self.body.strip().split("\n")[0][:120])

    @property
    def tags(self) -> list[str]:
        md = self.meta.get("metadata") or {}
        hermes = md.get("hermes") or {}
        return hermes.get("tags") or []

    @property
    def verify(self) -> Path | None:
        p = self.path.parent / "verify.py"
        return p if p.exists() else None

    @property
    def cassette(self) -> Path | None:
        c = self.path.parent / "cassettes"
        if c.exists() and any(c.iterdir()):
            return c
        return None

    @property
    def quarantined(self) -> bool:
        return (self.path.parent / ".quarantined").exists()

    def to_atom(self, importance: float = 0.6) -> dict:
        return {
            "kind": "procedure",
            "text": f"SKILL {self.name} ({self.category}): {self.description}",
            "importance": importance,
            "entities": [],
            "scope": "global",
        }

    def full_text(self, max_chars: int = 6000) -> str:
        body = self.body[:max_chars]
        if self.verify:
            body += f"\n\n## verify()\n{self.verify.read_text(encoding='utf-8')[:800]}"
        return body


class SkillRegistry:
    def __init__(self, genome_dir: Path | None = None) -> None:
        self.root = genome_dir or cfg.genome_path("skills")
        self._skills: dict[str, Skill] = {}
        self._scan()

    def _scan(self) -> None:
        if not self.root.exists():
            return
        for md in sorted(self.root.rglob("SKILL.md")):
            try:
                s = Skill(md)
                if not s.quarantined:
                    self._skills[s.name] = s
            except Exception:
                continue

    def all(self) -> list[Skill]:
        return list(self._skills.values())

    def get(self, name: str) -> Skill | None:
        return self._skills.get(name)

    def search(self, text: str, top: int = 5) -> list[Skill]:
        """Cheap lexical search over skill name/desc/tags (catalog lives in
        context as procedures only when retrieved — this is for the sandbox
        SDK / admin panel, NOT for the LLM prompt)."""
        words = set(re.findall(r"[a-z]{4,}", text.lower()))
        scored = []
        for s in self._skills.values():
            hay = " ".join([s.name, s.category, s.description.lower(),
                            " ".join(s.tags)]).lower()
            hit = sum(1 for w in words if w in hay)
            if hit:
                scored.append((hit, s))
        scored.sort(key=lambda x: -x[0])
        return [s for _, s in scored[:top]]

    def skill_atoms(self) -> list[dict]:
        """All skills as procedure atom candidates (ingested once, retrieved by demand)."""
        return [s.to_atom() for s in self._skills.values()]


_registry: SkillRegistry | None = None


def get_registry() -> SkillRegistry:
    global _registry
    if _registry is None:
        _registry = SkillRegistry()
    return _registry
