"""Document text extraction for System-SL chatbot attachments.

Supports: PDF (pypdf + optional OCR), DOCX (stdlib zipfile+xml), HTML (stdlib html.parser),
and plain text formats (txt, md, csv, etc.).
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import sys
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from html.parser import HTMLParser
from typing import Optional


# ---------------------------------------------------------------------------
# Data class
# ---------------------------------------------------------------------------

@dataclass
class DocumentText:
    """Result of extracting text from a document."""
    text: str
    pages: Optional[int]
    warnings: list[str]
    truncated: bool


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class UnsupportedFileError(ValueError):
    """Raised when the file type is not supported."""
    pass


class ExtractionError(RuntimeError):
    """Raised when text extraction fails."""
    pass


# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------

MAX_FILE_SIZE = 25 * 1024 * 1024  # 25 MB
MAX_TEXT_LENGTH = 40_000  # characters


# ---------------------------------------------------------------------------
# HTML text extractor (stdlib)
# ---------------------------------------------------------------------------

class _TextExtractor(HTMLParser):
    """Collects visible text from HTML, skipping scripts and styles."""

    def __init__(self) -> None:
        super().__init__()
        self._text_parts: list[str] = []
        self._skip = False
        self._skip_tags = {"script", "style", "noscript", "template"}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        if tag.lower() in self._skip_tags:
            self._skip = True

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in self._skip_tags:
            self._skip = False

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self._text_parts.append(data)

    def get_text(self) -> str:
        return " ".join(part.strip() for part in self._text_parts if part.strip())


# ---------------------------------------------------------------------------
# OCR fallback for scanned PDFs
# ---------------------------------------------------------------------------

OCR_CHECK_TEXT = "SYSTEM OCR CHECK 4729"


def _use_bundled_ocr_tools() -> Optional[str]:
    """Point OCR at the tools inside a frozen build; return the poppler directory.

    Nothing in the bundle is on PATH, so pytesseract and pdf2image cannot find
    these on their own. Windows keeps each tool in its own folder because their
    DLLs share names but not versions. A source install uses the system's tools.
    """
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle is None:
        return None

    import pytesseract

    root = Path(bundle)
    if os.name == "nt":
        tesseract = root / "ocr" / "tesseract" / "tesseract.exe"
        tessdata = root / "ocr" / "tessdata"
        poppler = root / "ocr" / "poppler"
    else:
        tesseract = root / "tesseract"
        tessdata = root / "tessdata"
        poppler = root

    pytesseract.pytesseract.tesseract_cmd = str(tesseract)
    # host_env() removes this again for programs launched outside the bundle.
    os.environ["TESSDATA_PREFIX"] = str(tessdata)
    return str(poppler)


def check_ocr() -> tuple[bool, str]:
    """Run a generated image-only PDF through extraction; return (ok, details).

    The release workflow calls this through ``--check-ocr``, because a build
    can contain every OCR file and still be unable to run them.
    """
    from PIL import Image, ImageDraw, ImageFont

    image = Image.new("RGB", (1400, 300), "white")
    ImageDraw.Draw(image).text(
        (40, 100), OCR_CHECK_TEXT, fill="black", font=ImageFont.load_default(size=72)
    )
    with tempfile.TemporaryDirectory() as directory:
        pdf = Path(directory) / "scanned.pdf"
        image.save(pdf, "PDF", resolution=150)
        try:
            result = extract_document(pdf)
        except (ExtractionError, UnsupportedFileError) as e:
            return False, f"OCR check failed: {e}"

    details = f"text={result.text!r} warnings={result.warnings!r}"
    if OCR_CHECK_TEXT in " ".join(result.text.split()):
        return True, f"OCR check passed: {details}"
    return False, f"OCR check failed: {details}"


def _extract_pdf_with_ocr(path: Path) -> DocumentText:
    """Extract text from scanned PDF using OCR (requires pytesseract + pdf2image)."""
    try:
        import pytesseract
        from pdf2image import convert_from_path
    except ImportError:
        raise ExtractionError(
            "OCR not available. Install tesseract + pytesseract + pdf2image for scanned PDF support."
        )

    warnings: list[str] = []
    try:
        poppler_path = _use_bundled_ocr_tools()
        # Convert PDF pages to images
        images = convert_from_path(str(path), poppler_path=poppler_path)
        page_count = len(images)
        text_parts: list[str] = []
        for i, image in enumerate(images):
            # OCR each page
            page_text = pytesseract.image_to_string(image)
            if page_text.strip():
                text_parts.append(f"[page {i + 1}]\n{page_text}")
            else:
                warnings.append(f"Page {i + 1}: OCR found no text")
        full_text = "\n\n".join(text_parts)
        if not full_text.strip():
            warnings.append("OCR extracted no text from any page")
        else:
            warnings.insert(0, "Text extracted via OCR (scanned document)")
        return _finalize(full_text, page_count, warnings)
    except Exception as e:
        raise ExtractionError(f"OCR failed: {e}") from e


# ---------------------------------------------------------------------------
# Extraction functions
# ---------------------------------------------------------------------------

def _extract_pdf(path: Path) -> DocumentText:
    """Extract text from PDF using pypdf, with OCR fallback for scanned pages."""
    try:
        from pypdf import PdfReader
    except ImportError as e:
        raise ExtractionError("pypdf not installed. Run: pip install pypdf") from e

    warnings: list[str] = []
    try:
        reader = PdfReader(str(path))
        page_count = len(reader.pages)
        text_parts: list[str] = []
        scanned_pages = 0
        for i, page in enumerate(reader.pages):
            page_text = page.extract_text() or ""
            if page_text.strip():
                text_parts.append(f"[page {i + 1}]\n{page_text}")
            else:
                warnings.append(f"Page {i + 1}: no extractable text (may be scanned)")
                scanned_pages += 1
        full_text = "\n\n".join(text_parts)
        
        # If all pages are scanned (or no text at all), try OCR as fallback
        if not full_text.strip():
            warnings.append("No text extracted from any page — attempting OCR fallback...")
            try:
                ocr_result = _extract_pdf_with_ocr(path)
                # Merge OCR warnings
                ocr_result.warnings = warnings + ocr_result.warnings
                return ocr_result
            except ExtractionError as ocr_error:
                # OCR not available or failed - return original result with warnings
                warnings.append(f"OCR fallback unavailable: {ocr_error}")
        
        if not full_text.strip():
            warnings.append("No text extracted from any page — document may be scanned/image-based (OCR not available)")
        return _finalize(full_text, page_count, warnings)
    except Exception as e:
        raise ExtractionError(f"Failed to extract PDF: {e}") from e


def _extract_docx(path: Path) -> DocumentText:
    """Extract text from DOCX using stdlib zipfile + XML parsing."""
    warnings: list[str] = []
    text_parts: list[str] = []
    try:
        with zipfile.ZipFile(path, "r") as z:
            if "word/document.xml" not in z.namelist():
                raise ExtractionError("Not a valid DOCX file (missing word/document.xml)")
            with z.open("word/document.xml") as f:
                xml_content = f.read()
        root = ET.fromstring(xml_content)
        # Word XML namespace
        ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
        for para in root.findall(".//w:p", ns):
            para_text_parts: list[str] = []
            for elem in para.iter():
                tag = elem.tag.split("}")[-1]  # strip namespace
                if tag == "t":
                    if elem.text:
                        para_text_parts.append(elem.text)
                elif tag == "tab":
                    para_text_parts.append("\t")
                elif tag == "br":
                    para_text_parts.append("\n")
            if para_text_parts:
                text_parts.append("".join(para_text_parts))
        full_text = "\n".join(text_parts)
        return _finalize(full_text, None, warnings)
    except zipfile.BadZipFile as e:
        raise ExtractionError("Not a valid DOCX file (bad zip)") from e
    except ET.ParseError as e:
        raise ExtractionError(f"Failed to parse DOCX XML: {e}") from e
    except Exception as e:
        raise ExtractionError(f"Failed to extract DOCX: {e}") from e


def _extract_html(path: Path) -> DocumentText:
    """Extract text from HTML using stdlib HTMLParser."""
    warnings: list[str] = []
    try:
        # Try UTF-8 first, fall back to latin-1
        for encoding in ("utf-8", "latin-1"):
            try:
                content = path.read_text(encoding=encoding)
                break
            except UnicodeDecodeError:
                continue
        else:
            content = path.read_text(encoding="utf-8", errors="replace")
            warnings.append("HTML file had encoding issues, used replacement characters")

        parser = _TextExtractor()
        parser.feed(content)
        full_text = parser.get_text()
        return _finalize(full_text, None, warnings)
    except Exception as e:
        raise ExtractionError(f"Failed to extract HTML: {e}") from e


def _extract_plain_text(path: Path) -> DocumentText:
    """Extract text from plain text files (txt, md, csv, log, etc.)."""
    warnings: list[str] = []
    try:
        for encoding in ("utf-8", "latin-1"):
            try:
                content = path.read_text(encoding=encoding)
                break
            except UnicodeDecodeError:
                continue
        else:
            content = path.read_text(encoding="utf-8", errors="replace")
            warnings.append("Text file had encoding issues, used replacement characters")
        return _finalize(content, None, warnings)
    except Exception as e:
        raise ExtractionError(f"Failed to read text file: {e}") from e


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

_EXTRACTORS = {
    ".pdf": _extract_pdf,
    ".docx": _extract_docx,
    ".html": _extract_html,
    ".htm": _extract_html,
    ".md": _extract_plain_text,
    ".txt": _extract_plain_text,
    ".csv": _extract_plain_text,
    ".log": _extract_plain_text,
    ".json": _extract_plain_text,
    ".xml": _extract_plain_text,
    ".rst": _extract_plain_text,
    ".tex": _extract_plain_text,
}


def extract_document(path: str | Path) -> DocumentText:
    """Extract text from a document file.

    Args:
        path: Path to the document file.

    Returns:
        DocumentText with extracted text, page count (if applicable),
        warnings, and truncation flag.

    Raises:
        UnsupportedFileError: If the file extension is not supported.
        ExtractionError: If extraction fails.
        ValueError: If file is too large or doesn't exist.
    """
    p = Path(path).expanduser().resolve()

    if not p.exists():
        raise ValueError(f"File not found: {p}")
    if not p.is_file():
        raise ValueError(f"Not a file: {p}")

    size = p.stat().st_size
    if size > MAX_FILE_SIZE:
        raise ValueError(f"File too large ({size / 1024 / 1024:.1f} MB > 25 MB limit)")

    suffix = p.suffix.lower()
    extractor = _EXTRACTORS.get(suffix)
    if extractor is None:
        raise UnsupportedFileError(
            f"Unsupported file type: {suffix}. "
            f"Supported: {', '.join(sorted(_EXTRACTORS.keys()))}"
        )

    return extractor(p)


def _finalize(text: str, pages: Optional[int], warnings: list[str]) -> DocumentText:
    """Apply truncation limit and return DocumentText."""
    truncated = False
    if len(text) > MAX_TEXT_LENGTH:
        text = text[:MAX_TEXT_LENGTH] + "\n…[truncated]…"
        truncated = True
        warnings.append(f"Extracted text truncated to {MAX_TEXT_LENGTH} characters")

    # Strip control characters except newline and tab
    cleaned = "".join(ch for ch in text if ch == "\n" or ch == "\t" or ch >= " ")
    # Collapse excessive blank lines (more than 2 consecutive)
    import re
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    cleaned = cleaned.strip()

    return DocumentText(
        text=cleaned,
        pages=pages,
        warnings=warnings,
        truncated=truncated,
    )


# ---------------------------------------------------------------------------
# Convenience: format for chat message
# ---------------------------------------------------------------------------

def format_for_chat(doc: DocumentText, filename: str) -> str:
    """Format extracted document text for inclusion in a chat message."""
    lines = [f"[uploaded document: {filename}"]
    if doc.pages is not None:
        lines[0] += f" — {doc.pages} page(s)"
    lines[0] += ", extracted text follows]"
    if doc.warnings:
        lines.append(f"[warnings: {'; '.join(doc.warnings)}]")
    lines.append(doc.text)
    return "\n".join(lines)