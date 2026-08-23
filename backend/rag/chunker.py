from __future__ import annotations
import re


def chunk_policy(policy_path: str) -> list[dict]:
    """Split policy.md on ## headers.

    Each chunk: {section_id, title, content, char_count}.
    section_id is derived from the leading number ("## 2. Purchase..." → "§2").
    """
    with open(policy_path, encoding="utf-8") as f:
        text = f.read()

    # Split on any line that opens a level-2 heading
    parts = re.split(r"\n(?=## )", text.strip())
    chunks: list[dict] = []

    for part in parts:
        part = part.strip()
        if not part.startswith("## "):
            continue

        lines = part.split("\n", 1)
        header = lines[0][3:].strip()            # drop "## "
        content = lines[1].strip() if len(lines) > 1 else ""

        # Extract section number e.g. "2." or "2.1." → §2 / §2.1
        m = re.match(r"^(\d+(?:\.\d+)?)[.\s]", header)
        section_id = f"§{m.group(1)}" if m else f"§{len(chunks) + 1}"

        chunks.append(
            {
                "section_id": section_id,
                "title": header,
                "content": content,
                "char_count": len(content),
            }
        )

    return chunks
