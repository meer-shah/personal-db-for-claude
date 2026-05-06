import tiktoken
import openpyxl

_enc = tiktoken.get_encoding("cl100k_base")
_MAX_TOKENS = 400  # leave headroom for the model's 256 word-piece limit


def parse_xlsx(file_path: str) -> list[dict]:
    """
    Parse an Excel workbook, iterating all sheets.
    Row 1 of each sheet is treated as column headers.
    Rows are batched into chunks that stay under _MAX_TOKENS so large sheets
    don't get silently truncated by the embedding model.

    Returns a list of dicts with keys:
        type        — always 'table'
        text        — structured key:value content
        sheet_name  — name of the source sheet
    """
    wb = openpyxl.load_workbook(file_path, data_only=True)
    chunks: list[dict] = []

    for sheet_name in wb.sheetnames:
        ws = wb[sheet_name]
        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            continue

        headers = [str(h).strip() if h is not None else "" for h in rows[0]]
        if not any(headers):
            continue

        header_line = "TABLE: " + " | ".join(h for h in headers if h)
        context_prefix = f"[Context: Sheet — {sheet_name}]\n\n"

        batch_lines: list[str] = []
        batch_tokens = len(_enc.encode(context_prefix + header_line))

        def _flush(lines: list[str]) -> None:
            if not lines:
                return
            text = context_prefix + header_line + "\n" + "\n".join(lines)
            chunks.append({"type": "table", "text": text, "sheet_name": sheet_name})

        for row in rows[1:]:
            if all(v is None for v in row):
                continue

            row_lines = []
            for h, v in zip(headers, row):
                if h:
                    row_lines.append(f"{h}: {str(v or '').strip()}")
            row_lines.append("---")

            row_tokens = len(_enc.encode("\n".join(row_lines)))

            if batch_lines and batch_tokens + row_tokens > _MAX_TOKENS:
                _flush(batch_lines)
                batch_lines = []
                batch_tokens = len(_enc.encode(context_prefix + header_line))

            batch_lines.extend(row_lines)
            batch_tokens += row_tokens

        _flush(batch_lines)

    return chunks
