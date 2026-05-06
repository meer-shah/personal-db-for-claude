import csv
import io


def parse_plain(file_path: str) -> list[dict]:
    """
    Parse .txt and .md files as a single text chunk.
    Parse .csv files as a structured TABLE chunk (DictReader).

    Returns a list of dicts with keys:
        type  — 'text' or 'table'
        text  — raw string content
    """
    lower = file_path.lower()

    if lower.endswith(".csv"):
        return _parse_csv(file_path)

    # .txt and .md
    with open(file_path, encoding="utf-8", errors="replace") as f:
        content = f.read().strip()

    if not content:
        return []
    return [{"type": "text", "text": content}]


def _parse_csv(file_path: str) -> list[dict]:
    with open(file_path, encoding="utf-8", errors="replace", newline="") as f:
        raw = f.read()

    reader = csv.DictReader(io.StringIO(raw))
    rows = list(reader)
    if not rows:
        return []

    headers = list(rows[0].keys())
    lines = ["TABLE: " + " | ".join(headers)]
    for row in rows:
        for h in headers:
            lines.append(f"{h}: {str(row.get(h, '') or '').strip()}")
        lines.append("---")

    return [{"type": "table", "text": "\n".join(lines)}]
