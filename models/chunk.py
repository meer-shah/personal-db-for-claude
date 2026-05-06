from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Chunk:
    text:             str
    chunk_type:       str            # 'text' | 'table'
    file_path:        str
    file_name:        str
    file_type:        str            # 'docx' | 'pdf' | 'xlsx' | 'pptx' | 'txt' | 'md' | 'csv'
    onedrive_item_id: str
    modified_date:    datetime
    created_date:     datetime
    content_hash:     str            # sha256 of text — used for change detection
    chunk_index:      int            # position within the source file
    author:           str | None = None
    page_number:      int | None = None   # PDF and Word
    slide_number:     int | None = None   # PowerPoint
    sheet_name:       str | None = None   # Excel
    vector:           list[float] = field(default_factory=list)  # 384-dim, set by Embedder
