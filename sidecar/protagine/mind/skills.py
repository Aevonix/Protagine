"""Skills: proven lessons written as Hermes skills (architecture 4.8 item 4; flag ``skills``, off by default).

An active lesson with at least ``PROMOTE_WINS`` verified wins and a win rate of at least
``PROMOTE_RATE`` becomes ``<dir>/protagine-<slug>/SKILL.md`` in the directory ``protagine init``
lists in Hermes' ``skills.external_dirs`` (``<instance>/skills``). Hermes' curator refuses
autonomous writes to external skills, so Protagine owns their lifecycle: a manifest in the
directory lists what it wrote, ``sync`` removes a skill whose lesson is retired, superseded,
erased or no longer promotable, and with the flag off (or lessons off) it removes every skill it
owns and touches nothing else. Every change bumps a generation (``mind_state``) the plugin reads to
clear Hermes' skills prompt cache, so a new session sees the change without a restart. Loads of
these skills (Hermes' ``on_skill_lifecycle``) are counted; retirement stays the lesson rule.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional
from protagine.util.temporal import now_utc

logger = logging.getLogger(__name__)

PREFIX, MANIFEST = "protagine-", ".protagine-skills.json"
PROMOTE_WINS, PROMOTE_RATE = 3, 0.7
GENERATION_KEY = "skills.generation"          # mind_state; level = the generation
LOADS_PREFIX = "skills.loaded:"               # mind_state; level = loads of that skill
DESCRIPTION_CHARS = 60                        # what Hermes' skills index shows of a description
NAME_CHARS = 64


def _slug(text: str) -> str:
    from .drives import slug
    return slug(text)[: NAME_CHARS - len(PREFIX)].strip("-") or "lesson"


def _write(path: Path, text: str) -> None:
    """Atomically: a session building its skills index never reads half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".skill-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


class Skills:
    def __init__(self, directory: Path | str, *, mind_state: Any, clock=None, enabled: bool = False) -> None:
        self.directory = Path(directory)
        self.mind_state = mind_state
        self.clock = clock or (lambda: now_utc())
        self.enabled = bool(enabled)

    # -- what is owned ----------------------------------------------------------------------

    def owned(self) -> Dict[str, str]:
        """Skill name -> lesson id, as the manifest lists them."""
        try:
            value = json.loads((self.directory / MANIFEST).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        skills = value.get("skills") if isinstance(value, dict) else None
        return {str(name): str(ident) for name, ident in (skills or {}).items()
                if str(name).startswith(PREFIX)} if isinstance(skills, dict) else {}

    def generation(self) -> int:
        entry = self.mind_state.get(GENERATION_KEY) if self.mind_state is not None else None
        return int(float((entry or {}).get("level") or 0))

    def loads(self) -> Dict[str, int]:
        if self.mind_state is None:
            return {}
        return {item["key"][len(LOADS_PREFIX):]: int(float(item.get("level") or 0))
                for item in self.mind_state.items(LOADS_PREFIX)}

    # -- promotion ---------------------------------------------------------------------------

    @staticmethod
    def promotable(lessons: Iterable[Any], tallies: Mapping[str, Mapping[str, int]]) -> List[Any]:
        """Active lessons with at least ``PROMOTE_WINS`` verified wins at a rate of at least ``PROMOTE_RATE``."""
        chosen = []
        for lesson in lessons:
            counts = tallies.get(lesson.id) or {}
            uses, wins = int(counts.get("uses") or 0), int(counts.get("wins") or 0)
            if lesson.status == "active" and wins >= PROMOTE_WINS and uses and wins / uses >= PROMOTE_RATE:
                chosen.append(lesson)
        return chosen

    def _names(self, lessons: List[Any], owned: Mapping[str, str]) -> Dict[str, Any]:
        """A stable name per promoted lesson: its title's slug, with its id when two titles collide."""
        names: Dict[str, Any] = {}
        by_lesson = {ident: name for name, ident in owned.items()}
        for lesson in sorted(lessons, key=lambda item: item.id):
            name = by_lesson.get(lesson.id) or f"{PREFIX}{_slug(lesson.title)}"
            if name in names or (name in owned and owned[name] != lesson.id):
                name = f"{PREFIX}{_slug(lesson.title)[:NAME_CHARS - len(PREFIX) - 11]}-{lesson.id[2:].lower()}"
            names[name] = lesson
        return names

    @staticmethod
    def render(name: str, lesson: Any, counts: Mapping[str, int]) -> str:
        description = f"When {lesson.when_to_use}"
        if len(description) > DESCRIPTION_CHARS:
            description = description[: DESCRIPTION_CHARS - 3].rstrip() + "..."
        wins, uses = int(counts.get("wins") or 0), int(counts.get("uses") or 0)
        return "\n".join([
            "---", f"name: {name}", f"description: {json.dumps(description)}", "---", "",
            f"# {lesson.title}", "", f"When {lesson.when_to_use}:", "", lesson.content, "",
            f"This is a {lesson.kind} Protagine learned from verified results (lesson {lesson.id}, "
            f"{wins} wins in {uses} verified uses). Protagine owns this file and removes it when the lesson is "
            "retired; apply it only when the request matches, and the owner's word comes first.", ""])

    def sync(self, lessons: Iterable[Any], tallies: Mapping[str, Mapping[str, int]],
             now: Optional[datetime] = None) -> Dict[str, Any]:
        """Write the promotable lessons' skills and remove every other owned one; bump the generation on
        any change. With the flag off every owned skill goes. Nothing outside the manifest is touched."""
        now = now or self.clock()
        owned = self.owned()
        wanted = self._names(self.promotable(lessons, tallies), owned) if self.enabled else {}
        written: List[str] = []
        removed: List[str] = []
        for name, ident in owned.items():
            if name in wanted and wanted[name].id == ident:
                continue
            target = (self.directory / name).resolve()
            if target.parent == self.directory.resolve() and target.name.startswith(PREFIX):
                shutil.rmtree(target, ignore_errors=True)
            removed.append(name)
        for name, lesson in wanted.items():
            path = self.directory / name / "SKILL.md"
            if owned.get(name) == lesson.id and path.is_file():
                continue
            _write(path, self.render(name, lesson, tallies.get(lesson.id) or {}))
            written.append(name)
        if written or removed:
            _write(self.directory / MANIFEST, json.dumps({"skills": {name: lesson.id for name, lesson in wanted.items()}},
                                                         indent=1, sort_keys=True) + "\n")
            if self.mind_state is not None:
                self.mind_state.set(GENERATION_KEY, level=float(self.generation() + 1),
                                    causes=[f"skill:{name}" for name in (written + removed)][:5], now=now)
        return {"written": sorted(written), "removed": sorted(removed), "generation": self.generation()}

    # -- use --------------------------------------------------------------------------------

    def record_use(self, *, skill: str, session_id: str | None = None, task_id: str | None = None,
                   now: Optional[datetime] = None) -> Optional[int]:
        """One load of one of Protagine's skills (Hermes' ``on_skill_lifecycle`` ``loaded``); others are not
        counted."""
        skill = str(skill or "").strip()
        if not skill.startswith(PREFIX) or len(skill) > NAME_CHARS or self.mind_state is None:
            return None
        cause = f"session:{session_id}" if session_id else f"task:{task_id}" if task_id else None
        return int(self.mind_state.bump(f"{LOADS_PREFIX}{skill}", 1.0, cap=1e9, causes=[cause] if cause else None,
                                        now=now or self.clock()))

    def state(self) -> Dict[str, Any]:
        return {"enabled": self.enabled, "generation": self.generation(), "owned": sorted(self.owned())}


__all__ = ["GENERATION_KEY", "MANIFEST", "PREFIX", "PROMOTE_RATE", "PROMOTE_WINS", "Skills"]
