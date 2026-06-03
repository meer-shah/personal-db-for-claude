import gc

import tiktoken
import openpyxl
import openpyxl.reader.excel as _xl_reader

_enc = tiktoken.get_encoding("cl100k_base")
_MAX_TOKENS = 400  # leave headroom for the model's 256 word-piece limit


# --- Work around an openpyxl 3.1.x crash on chartsheets ------------------------
# ExcelReader.read_chartsheet() does `rels.find(...)`, but `rels` is a plain list
# when the chartsheet has no _rels file -> AttributeError that aborts the ENTIRE
# load_workbook(). A single chartsheet would otherwise make a whole workbook
# unparseable and push it through the 3x-retry quarantine path (re-allocating the
# workbook each attempt -- a real contributor to the RSS spikes observed in prod).
# Wrap it so a problematic chartsheet is skipped while the data sheets still load.
_orig_read_chartsheet = _xl_reader.ExcelReader.read_chartsheet


def _safe_read_chartsheet(self, sheet, rel):
    try:
        return _orig_read_chartsheet(self, sheet, rel)
    except Exception:
        return  # skip the unreadable chartsheet; keep loading the workbook


_xl_reader.ExcelReader.read_chartsheet = _safe_read_chartsheet
# ------------------------------------------------------------------------------


def parse_xlsx(file_path: str) -> list[dict]:
    """
    Parse an Excel workbook, iterating all data sheets. Row 1 of each sheet is
    treated as column headers. Rows are batched into chunks that stay under
    _MAX_TOKENS so large sheets are not truncated by the embedding model.

    read_only=True keeps it streaming (no full workbook tree in memory).
    Chart-only / non-data sheets are skipped. Memory is released via
    wb.close() + gc.collect() on EVERY exit path (including the error path), so a
    run of heavy or broken workbooks does not accumulate RSS until the indexer
    OOM-kills.

    Returns a list of dicts: {type: 'table', text: ..., sheet_name: ...}
    """
    wb = None
    chunks: list[dict] = []
    try:
        wb = openpyxl.load_workbook(file_path, read_only=True, data_only=True)

        for sheet_name in wb.sheetnames:
            ws = wb[sheet_name]
            # Skip chart-only / non-data sheets: a Chartsheet has no iter_rows().
            if not hasattr(ws, "iter_rows"):
                continue

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

            def _flush(lines, _sheet=sheet_name, _prefix=context_prefix, _hdr=header_line):
                if not lines:
                    return
                text = _prefix + _hdr + "\n" + "\n".join(lines)
                chunks.append({"type": "table", "text": text, "sheet_name": _sheet})

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
        if wb is not None:
            try:
                wb.close()
            except Exception:
                pass
        gc.collect()

    return chunks