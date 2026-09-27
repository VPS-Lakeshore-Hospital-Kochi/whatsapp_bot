"""Load the approved knowledge base from Markdown files.

Every file needs a front-matter header naming its owner, audience and review date. Files that
are not `status: approved`, or whose `review_by` date has passed, are NOT loaded, so stale
content can't be quoted to anyone. This is the single most effective control against wrong
answers: most "hallucinations" in practice are the bot faithfully quoting an outdated document.
"""

import logging
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

AUDIENCES = ("patient", "staff", "clinician")


@dataclass(frozen=True)
class Chunk:
    id: str  # e.g. "patient/visiting-hours#2"
    doc_title: str
    section: str
    text: str
    audience: frozenset[str]
    owner: str
    version: str
    review_by: date

    def citation(self) -> str:
        return f"{self.doc_title} (v{self.version})"


_FRONT_MATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)


def load_knowledge(root: Path, today: date | None = None) -> list[Chunk]:
    today = today or date.today()
    chunks: list[Chunk] = []
    for path in sorted(root.rglob("*.md")):
        if path.name.lower() == "readme.md":
            continue
        doc_id = path.relative_to(root).with_suffix("").as_posix()
        match = _FRONT_MATTER.match(path.read_text(encoding="utf-8"))
        if not match:
            log.warning("Skipping %s: no front matter", doc_id)
            continue
        meta = yaml.safe_load(match.group(1)) or {}
        problem = _validate(meta, today)
        if problem:
            log.warning("Skipping %s: %s", doc_id, problem)
            continue
        for i, (section, text) in enumerate(_split_sections(match.group(2))):
            chunks.append(Chunk(
                id=f"{doc_id}#{i}",
                doc_title=str(meta["title"]),
                section=section,
                text=text,
                audience=frozenset(meta["audience"]),
                owner=str(meta["owner"]),
                version=str(meta["version"]),
                review_by=meta["review_by"],
            ))
    log.info("Loaded %d knowledge chunks from %s", len(chunks), root)
    return chunks


def _validate(meta: dict, today: date) -> str | None:
    for key in ("title", "owner", "audience", "version", "review_by", "status"):
        if key not in meta:
            return f"missing '{key}'"
    if meta["status"] != "approved":
        return f"status is '{meta['status']}'"
    if not isinstance(meta["review_by"], date):
        return "review_by must be a YYYY-MM-DD date"
    if meta["review_by"] < today:
        return f"review date {meta['review_by']} has passed"
    bad = set(meta["audience"]) - set(AUDIENCES)
    if bad:
        return f"unknown audience {sorted(bad)}"
    return None


def _split_sections(body: str) -> list[tuple[str, str]]:
    """One chunk per '## ' section; text before the first heading is its own chunk."""
    sections: list[tuple[str, str]] = []
    current_title, lines = "Overview", []
    for line in body.splitlines():
        if line.startswith("## "):
            if "".join(lines).strip():
                sections.append((current_title, "\n".join(lines).strip()))
            current_title, lines = line[3:].strip(), []
        elif not line.startswith("# "):
            lines.append(line)
    if "".join(lines).strip():
        sections.append((current_title, "\n".join(lines).strip()))
    return sections
