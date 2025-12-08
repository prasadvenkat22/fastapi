from fastapi import APIRouter, HTTPException, File, UploadFile, Form
from typing import List, Optional, Dict, Any
import io
import os
import csv
from pydantic import BaseModel
from .llm_integration import run_rag, DocumentRequest

try:
    from PyPDF2 import PdfReader
except Exception:
    PdfReader = None

router = APIRouter(prefix="/api/genai", tags=["GENAI"])


class DocumentResponse(BaseModel):
    id: Optional[str]
    content: str
    metadata: Dict = {}
    score: Optional[float] = None


class QueryResponse(BaseModel):
    results: List[DocumentResponse]


@router.post("/query", response_model=QueryResponse)
async def genai_query(request: DocumentRequest):
    """Accept a structured request and return structured results.
    Delegates to `run_rag` in `llm_integration`.
    """
    try:
        results = await run_rag(request)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    # Normalize results to plain dicts so pydantic will validate across modules
    normalized = []
    for r in results:
        if hasattr(r, "model_dump"):
            normalized.append(r.model_dump())
        elif hasattr(r, "dict"):
            normalized.append(r.dict())
        else:
            normalized.append(r)

    return QueryResponse(results=normalized)


def _infer_column_types(rows: List[List[str]], max_sample: int = 100) -> List[str]:
    types: List[str] = []
    cols = max((len(r) for r in rows), default=0)
    sample_rows = rows[1:1+max_sample] if len(rows) > 1 else []
    for c in range(cols):
        col_vals = [r[c] for r in sample_rows if len(r) > c]
        inferred = "string"
        if not col_vals:
            inferred = "empty"
        else:
            is_int = True
            is_float = True
            is_date = True
            for v in col_vals:
                v = v.strip()
                if v == "":
                    continue
                # int
                try:
                    int(v)
                except Exception:
                    is_int = False
                # float
                try:
                    float(v)
                except Exception:
                    is_float = False
                # date (best-effort)
                try:
                    from dateutil.parser import parse as _parse

                    _parse(v)
                except Exception:
                    is_date = False
            if is_int:
                inferred = "integer"
            elif is_float:
                inferred = "float"
            elif is_date:
                inferred = "date"
            else:
                inferred = "string"
        types.append(inferred)
    return types


def _csv_to_markdown_sample(reader: List[List[str]], header: List[str], rows_to_include: List[List[str]]) -> str:
    md_lines: List[str] = []
    md_header = "| " + " | ".join(header) + " |"
    md_sep = "| " + " | ".join(["---"] * len(header)) + " |"
    md_lines.append(md_header)
    md_lines.append(md_sep)
    for r in rows_to_include:
        if r == []:
            cells = ["..."] * len(header)
        else:
            cells = [str(r[i]) if i < len(r) else "" for i in range(len(header))]
        md_lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(md_lines)


@router.post("/query/upload", response_model=QueryResponse)
async def genai_query_upload(
    files: List[UploadFile] = File(...),
    query: str = Form(...),
    top_k: int = Form(4),
    summarize: bool = Form(False),
):
    """Accept file uploads (PDF/TXT/CSV/DOCX/Images) with a text `query` and send uploaded documents to RAG/LLM.
    - `summarize` (form flag): when true, produce a local CSV summary (infer column types + markdown sample) and send that summary to the LLM instead of raw rows.
    """
    uploads: List[Dict[str, Any]] = []

    # Configurable env overrides
    MAX_CSV_ROWS = int(os.getenv("GENAI_MAX_CSV_ROWS", "500"))
    MAX_CHARS_TO_SEND = int(os.getenv("GENAI_MAX_CHARS_TO_SEND", "30000"))
    HEAD_SAMPLE = int(os.getenv("GENAI_CSV_HEAD_SAMPLE", "10"))
    TAIL_SAMPLE = int(os.getenv("GENAI_CSV_TAIL_SAMPLE", "10"))
    MAX_TABLE_ROWS = int(os.getenv("GENAI_CSV_TABLE_ROWS", "20"))

    for f in files:
        data = await f.read()
        text = ""
        fname = getattr(f, "filename", None) or "uploaded"
        lower = fname.lower()

        # PDF
        if lower.endswith(".pdf") and PdfReader is not None:
            try:
                reader = PdfReader(io.BytesIO(data))
                pages: List[str] = []
                for p in reader.pages:
                    try:
                        pages.append(p.extract_text() or "")
                    except Exception:
                        pass
                text = "\n\n".join(pages)
            except Exception:
                try:
                    text = data.decode("utf-8", errors="ignore")
                except Exception:
                    text = ""

        # TXT
        elif lower.endswith(".txt"):
            try:
                text = data.decode("utf-8", errors="ignore")
            except Exception:
                text = ""

        # CSV with advanced summarization / sampling
        elif lower.endswith(".csv"):
            try:
                decoded = data.decode("utf-8", errors="ignore")
                reader = list(csv.reader(decoded.splitlines()))
                total_rows = len(reader)
                header = reader[0] if total_rows > 0 else []

                if summarize:
                    col_types = _infer_column_types(reader)
                    type_lines = []
                    for i, t in enumerate(col_types[: len(header)]):
                        type_lines.append(f"- {header[i]}: {t}")
                    type_summary = "\n".join(type_lines)

                    sample_rows = []
                    sample_rows.append(header)
                    sample_rows.extend(reader[1:1 + HEAD_SAMPLE])
                    md = _csv_to_markdown_sample(reader, header, sample_rows)

                    summary_header = f"CSV file '{fname}' with {total_rows} rows and {len(header)} columns.\n"
                    combined = summary_header + "\nColumn types:\n" + type_summary + "\n\nSample table:\n" + md

                    if len(combined) > MAX_CHARS_TO_SEND:
                        combined = combined[:MAX_CHARS_TO_SEND] + "\n\n[Truncated due to size]"
                    text = combined
                else:
                    if total_rows <= MAX_CSV_ROWS:
                        rows_to_include = reader
                    else:
                        head_rows = reader[1:1 + HEAD_SAMPLE] if total_rows > 1 else []
                        tail_rows = reader[-TAIL_SAMPLE:]
                        rows_to_include = [header] + head_rows + [[]] + tail_rows

                    rows_text = []
                    for row in rows_to_include:
                        if row == []:
                            rows_text.append("...")
                        else:
                            rows_text.append(", ".join(cell for cell in row))
                    text_block = "\n".join(rows_text)
                    summary_header = f"CSV file '{fname}' with {total_rows} rows and {len(header)} columns.\n\n"
                    combined = summary_header + text_block
                    if len(combined) > MAX_CHARS_TO_SEND:
                        combined = combined[:MAX_CHARS_TO_SEND] + "\n\n[Truncated due to size]"
                    text = combined
            except Exception:
                try:
                    text = data.decode("utf-8", errors="ignore")
                except Exception:
                    text = ""

        # DOCX
        elif lower.endswith(".docx"):
            try:
                from docx import Document

                doc = Document(io.BytesIO(data))
                parts: List[str] = []
                for para in doc.paragraphs:
                    parts.append(para.text)
                text = "\n\n".join(parts)
            except Exception:
                try:
                    text = data.decode("utf-8", errors="ignore")
                except Exception:
                    text = ""

        # Images (OCR) - optional
        elif any(lower.endswith(ext) for ext in (".png", ".jpg", ".jpeg", ".tiff", ".bmp")):
            try:
                from PIL import Image
                import pytesseract

                img = Image.open(io.BytesIO(data))
                text = pytesseract.image_to_string(img)
            except Exception:
                text = "[binary image content omitted]"

        else:
            try:
                text = data.decode("utf-8", errors="ignore")
            except Exception:
                text = ""

        uploads.append({"content": text, "metadata": {"filename": fname}})
        await f.close()

    request = DocumentRequest(query=query, top_k=top_k)
    try:
        results = await run_rag(request, uploads=uploads)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    # Normalize results for pydantic
    normalized: List[Dict[str, Any]] = []
    for r in results:
        if hasattr(r, "model_dump"):
            normalized.append(r.model_dump())
        elif hasattr(r, "dict"):
            normalized.append(r.dict())
        else:
            normalized.append(r)

    return QueryResponse(results=normalized)
