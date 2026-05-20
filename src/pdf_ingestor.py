"""
Extracts text from ShiftCare per-participant PDF reports for de-identification.

PDF filenames must follow the pattern produced by the scraper: YYYY-MM-DD-PART-XXX.pdf
"""
from __future__ import annotations

import logging
import re
from datetime import date
from pathlib import Path

logger = logging.getLogger(__name__)

_PDF_PATTERN = re.compile(r"^(\d{4}-\d{2}-\d{2})-(PART-\d+)\.pdf$", re.IGNORECASE)


def extract_text(pdf_path: Path) -> str:
    """Extract all text from a PDF using pdfplumber."""
    import pdfplumber  # deferred — only required when PDFs are present
    pages: list[str] = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text()
            if text:
                pages.append(text)
    result = "\n".join(pages)
    logger.debug("Extracted %d chars from %s", len(result), pdf_path.name)
    return result


def iter_pdfs(input_dir: Path) -> list[tuple[str, date, str, Path]]:
    """
    Return (ref_code, report_date, text, path) for every YYYY-MM-DD-PART-XXX.pdf
    found in input_dir.  Files with unexpected names, extraction errors, or empty
    text are skipped with a warning.
    """
    input_dir = Path(input_dir)
    results: list[tuple[str, date, str, Path]] = []
    for pdf_path in sorted(input_dir.glob("*.pdf")):
        m = _PDF_PATTERN.match(pdf_path.name)
        if not m:
            logger.warning("Skipping PDF with unexpected filename: %s", pdf_path.name)
            continue
        report_date = date.fromisoformat(m.group(1))
        ref_code = m.group(2).upper()
        try:
            text = extract_text(pdf_path)
        except Exception as exc:  # pylint: disable=broad-except
            logger.error("Could not extract text from %s: %s", pdf_path.name, exc)
            continue
        if not text.strip():
            logger.warning("PDF %s produced no extractable text — skipping", pdf_path.name)
            continue
        results.append((ref_code, report_date, text, pdf_path))
    logger.info("PDF ingest: %d usable file(s) from %s", len(results), input_dir)
    return results
