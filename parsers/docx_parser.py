from docx import Document
from docx.oxml.ns import qn


def _cell_text(tc_element) -> str:
    """Extract text from a <w:tc> element without descending into nested cells."""
    parts = []
    for t in tc_element.findall(".//" + qn("w:t")):
        parts.append(t.text or "")
    return "".join(parts).strip()


def _para_text(p_element) -> str:
    """Extract text from a <w:p> element (includes text inside text boxes/drawings)."""
    return "".join(t.text or "" for t in p_element.findall(".//" + qn("w:t"))).strip()


def parse_docx(file_path: str) -> list[dict]:
    """
    Parse a .docx file, walking the document body in element order so that
    tables are detected mid-document. Tables become their own chunk with the
    preceding paragraph as context. Header/footer text (a separate document
    part, not in the body) is also captured so documents whose content lives in
    a header or footer are not indexed as empty.

    Returns a list of dicts with keys: type ('text'|'table'), text.
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
            if text_buffer:
                chunks.append({"type": "text", "text": "\n".join(text_buffer)})

            rows = element.findall(".//" + qn("w:tr"))
            if not rows:
                text_buffer = []
                continue

            raw_headers = [_cell_text(c) for c in rows[0].findall(".//" + qn("w:tc"))]
            headers = [h if h else f"Col{i+1}" for i, h in enumerate(raw_headers)]
            table_lines = ["TABLE: " + " | ".join(headers)]

            for row in rows[1:]:
                cells = [_cell_text(c) for c in row.findall(".//" + qn("w:tc"))]
                for h, v in zip(headers, cells):
                    table_lines.append(f"{h}: {v}")
                table_lines.append("---")

            context = text_buffer[-1] if text_buffer else ""
            table_text = "\n".join(table_lines)
            if context:
                table_text = f"[Context: {context}]\n\n{table_text}"

            chunks.append({"type": "table", "text": table_text})
            text_buffer = []

    if text_buffer:
        chunks.append({"type": "text", "text": "\n".join(text_buffer)})

    # Headers/footers live in separate document parts (not doc.element.body), so
    # the body walk above misses them entirely. Capture unique header/footer text
    # so docs whose only/extra content is a letterhead, legal boilerplate,
    # classification marking, or one-line memo are not indexed as empty.
    hf_parts: list[str] = []
    seen_hf: set[str] = set()
    try:
        for section in doc.sections:
            for label, hf in (("Header", section.header), ("Footer", section.footer)):
                txt = "\n".join(
                    p.text.strip() for p in hf.paragraphs if p.text and p.text.strip()
                )
                if txt and txt not in seen_hf:
                    seen_hf.add(txt)
                    hf_parts.append(f"[{label}] {txt}")
    except Exception:
        pass
    if hf_parts:
        chunks.append({"type": "text", "text": "\n".join(hf_parts)})

    return chunks