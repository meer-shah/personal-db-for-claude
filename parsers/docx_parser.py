from docx import Document
from docx.oxml.ns import qn


def _cell_text(tc_element) -> str:
    """Extract text from a <w:tc> element without descending into nested cells."""
    parts = []
    for t in tc_element.findall(".//" + qn("w:t")):
        parts.append(t.text or "")
    return "".join(parts).strip()


def _para_text(p_element) -> str:
    """Extract text from a <w:p> element."""
    return "".join(t.text or "" for t in p_element.findall(".//" + qn("w:t"))).strip()


def parse_docx(file_path: str) -> list[dict]:
    """
    Parse a .docx file, walking the document body in element order so that
    tables are detected mid-document (via <w:tbl> tags) rather than lost
    between paragraphs. Tables are emitted as their own chunk; the last
    paragraph before each table is prepended as context so the table chunk
    is self-contained and searchable by topic.

    Returns a list of dicts with keys:
        type  — 'text' or 'table'
        text  — raw string content
    """
    doc = Document(file_path)
    chunks: list[dict] = []
    text_buffer: list[str] = []

    for element in doc.element.body:
        tag = element.tag.split("}")[1] if "}" in element.tag else element.tag

        if tag == "p":
            para = _para_text(element)
            if para:
                text_buffer.append(para)

        elif tag == "tbl":
            # Flush accumulated text before the table
            if text_buffer:
                chunks.append({"type": "text", "text": "\n".join(text_buffer)})

            rows = element.findall(".//" + qn("w:tr"))
            if not rows:
                text_buffer = []
                continue

            raw_headers = [
                _cell_text(c)
                for c in rows[0].findall(".//" + qn("w:tc"))
            ]
            # Fill empty header cells with placeholders so every column is named
            headers = [h if h else f"Col{i+1}" for i, h in enumerate(raw_headers)]
            table_lines = ["TABLE: " + " | ".join(headers)]

            for row in rows[1:]:
                cells = [
                    _cell_text(c)
                    for c in row.findall(".//" + qn("w:tc"))
                ]
                for h, v in zip(headers, cells):
                    table_lines.append(f"{h}: {v}")
                table_lines.append("---")

            # Prepend the last paragraph as context so the table is self-contained
            context = text_buffer[-1] if text_buffer else ""
            table_text = "\n".join(table_lines)
            if context:
                table_text = f"[Context: {context}]\n\n{table_text}"

            chunks.append({"type": "table", "text": table_text})
            text_buffer = []

    # Flush any remaining text after the last element
    if text_buffer:
        chunks.append({"type": "text", "text": "\n".join(text_buffer)})

    return chunks
