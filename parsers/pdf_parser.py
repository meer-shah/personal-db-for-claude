import pdfplumber


def _bbox_overlaps(x0, y0, x1, y1, bbox) -> bool:
    bx0, by0, bx1, by1 = bbox
    return not (x1 <= bx0 or x0 >= bx1 or y1 <= by0 or y0 >= by1)


def parse_pdf(file_path: str) -> list[dict]:
    """
    Parse a text-based PDF, extracting tables and non-table text per page.
    Table regions are excluded from the text pass to avoid double-extraction.

    Returns a list of dicts with keys:
        type  — 'text' or 'table'
        text  — raw string content
        page  — 1-based page number
    """
    chunks: list[dict] = []

    with pdfplumber.open(file_path) as pdf:
        for page_num, page in enumerate(pdf.pages, start=1):
            tables = page.find_tables()
            table_bboxes = [t.bbox for t in tables]

            # Extract non-table text first so we can use it as context for tables
            if table_bboxes:
                filtered = page.filter(
                    lambda obj: obj.get("object_type") == "char"
                    and not any(
                        _bbox_overlaps(
                            obj.get("x0", 0), obj.get("top", 0),
                            obj.get("x1", 0), obj.get("bottom", 0),
                            b,
                        )
                        for b in table_bboxes
                    )
                )
                page_text = filtered.extract_text()
            else:
                page_text = page.extract_text()

            # Use the last non-empty line of page text as context for tables on this page
            context = ""
            if page_text and page_text.strip():
                non_empty = [ln.strip() for ln in page_text.strip().splitlines() if ln.strip()]
                if non_empty:
                    context = non_empty[-1]

            # Extract table chunks, prepending page context
            for table in tables:
                rows = table.extract()
                if not rows:
                    continue
                headers = [str(h or "").strip() for h in rows[0]]
                lines = ["TABLE: " + " | ".join(headers)]
                for row in rows[1:]:
                    for h, v in zip(headers, row):
                        lines.append(f"{h}: {str(v or '').strip()}")
                    lines.append("---")
                table_text = "\n".join(lines)
                if context:
                    table_text = f"[Context: {context}]\n\n{table_text}"
                chunks.append({"type": "table", "text": table_text, "page": page_num})

            if page_text and page_text.strip():
                chunks.append({"type": "text", "text": page_text.strip(), "page": page_num})

            # Drop pdfplumber's per-page cache (_objects, _layout, _edges, …)
            # before moving to the next page. Without this, large PDFs hold
            # every page's parsed object tree in memory until the function
            # returns — which under threaded load adds up to GiBs of RSS
            # because Python's GC runs less aggressively under contention.
            page.flush_cache()

    return chunks
