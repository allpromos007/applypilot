"""Render tailored text into ATS-friendly Word (.docx) documents.

ATS parsers read single-column documents with standard headings reliably, and
often garble tables, text boxes, headers/footers, columns and images. So the
renderer deliberately produces the plainest possible structure:
    - one column, body text only (nothing in page headers/footers)
    - standard section headings as real Word headings
    - "- " lines become real Word bullets
    - a common font (Calibri 11)

Contact placeholders ([EMAIL], [PHONE], ...) are replaced with the real values
here, at the last possible moment, so real contact details never pass through
the model.
"""

from __future__ import annotations

import re
from pathlib import Path

from docx import Document
from docx.shared import Pt

from app.tools.text_analysis import HEADING_KEYWORDS

PLACEHOLDER_KEYS = {
    "[EMAIL]": "email",
    "[PHONE]": "phone",
    "[LINKEDIN]": "linkedin",
    "[GITHUB]": "github",
}
_BULLET = re.compile(r"^\s*(?:[-*\u2022])\s+")
_MD_NOISE = re.compile(r"(\*\*|__|`)")


def fill_contact_placeholders(text: str, contact: dict[str, str]) -> str:
    """Swap [EMAIL]-style placeholders for real values; drop ones we don't have."""
    for placeholder, key in PLACEHOLDER_KEYS.items():
        text = text.replace(placeholder, contact.get(key, ""))
    # Tidy separators left behind by removed placeholders: "a |  | b" -> "a | b".
    text = re.sub(r"(\s*[|\u2022\u00b7]\s*){2,}", " | ", text)
    return re.sub(r"[ \t]*[|\u2022\u00b7][ \t]*$", "", text, flags=re.M)


def _is_heading(line: str) -> bool:
    """Markdown '#' headings, or ALL-CAPS lines naming a standard resume section."""
    if line.lstrip().startswith("#"):
        return True
    bare = line.strip().rstrip(":")
    return (
        bare.isupper()
        and len(bare.split()) <= 4
        and any(keyword in bare.lower() for keyword, _ in HEADING_KEYWORDS)
    )


def render_ats_docx(text: str, contact: dict[str, str], output_path: Path) -> Path:
    """Write `text` to `output_path` as a single-column ATS-friendly .docx."""
    text = fill_contact_placeholders(_MD_NOISE.sub("", text), contact)
    doc = Document()
    style = doc.styles["Normal"]
    style.font.name = "Calibri"
    style.font.size = Pt(11)

    lines = [ln.rstrip() for ln in text.strip().splitlines()]
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        if _BULLET.match(line):
            doc.add_paragraph(_BULLET.sub("", line), style="List Bullet")
        elif _is_heading(line) and i > 0:
            doc.add_heading(
                line.strip().lstrip("#").strip().rstrip(":").upper(), level=2
            )
        else:
            para = doc.add_paragraph(line.strip())
            para.paragraph_format.space_after = Pt(2)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(output_path))
    return output_path
