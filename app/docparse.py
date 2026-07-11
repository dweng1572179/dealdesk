"""Document -> plaintext for term extraction. PDF via pypdf, .docx via python-docx,
everything else decoded as text. Both libs are optional: a missing one degrades
that format with a clear message instead of crashing the upload."""
import io


def extract_text(filename: str, data: bytes) -> str:
    name = filename.lower()
    if name.endswith(".pdf"):
        return _pdf(data)
    if name.endswith(".docx"):
        return _docx(data)
    # .txt, .md, .eml, .csv, unknown — best-effort decode
    return data.decode("utf-8", errors="replace")


def _pdf(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        return "[PDF parsing needs pypdf — paste the text instead, or `pip install pypdf`.]"
    try:
        reader = PdfReader(io.BytesIO(data))
        return "\n".join((page.extract_text() or "") for page in reader.pages).strip()
    except Exception as e:  # noqa: BLE001 — a scanned/broken PDF shouldn't kill the upload
        return f"[Could not read PDF: {e}. If it's a scan, it has no selectable text.]"


def _docx(data: bytes) -> str:
    try:
        import docx  # python-docx
    except ImportError:
        return "[.docx parsing needs python-docx — paste the text instead, or `pip install python-docx`.]"
    try:
        d = docx.Document(io.BytesIO(data))
        return "\n".join(p.text for p in d.paragraphs).strip()
    except Exception as e:  # noqa: BLE001
        return f"[Could not read .docx: {e}]"


def demo() -> None:
    assert extract_text("note.txt", b"Loan amount $5,000,000") == "Loan amount $5,000,000"
    assert "pypdf" in _pdf(b"%PDF-broken") or "Could not read" in _pdf(b"%PDF-broken")
    print("docparse.demo OK")


if __name__ == "__main__":
    demo()
