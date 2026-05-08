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
    # read_only=True switches openpyxl to a streaming SAX reader: it does NOT
    # materialize the full workbook tree, skips pivot caches entirely, and
    # releases each row as it's yielded. This is the dominant fix for the
    # ~5 GiB/run leak memray traced to openpyxl.get_rel + pivot_caches.
    # data_only=True returns cached cell values rather than formula strings.
    wb = openpyxl.load_workbook(file_path, read_only=True, data_only=True)
    chunks: list[dict] = []

    try:
        for sheet_name in wb.sheetnames:
            ws = wb[sheet_name]
            row_iter = ws.iter_rows(values_only=True)

            try:
                header_row = next(row_iter)
            except StopIteration:
                continue

            headers = [str(h).strip() if h is not None else "" for h in header_row]
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

            for row in row_iter:
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
    finally:
        # read_only mode opens a streaming file handle that doesn't auto-close;
        # explicit close releases it immediately rather than waiting on GC.
        wb.close()

    return chunks
