"""Isolated, bounded text-only PDF extraction. Never executes PDF actions."""
import json
from pathlib import Path
import sys

MAX_PAGES = 40
MAX_TEXT = 80000


def extract(path):
    from pypdf import PdfReader
    reader = PdfReader(path, strict=True)
    if reader.is_encrypted:
        raise ValueError('Password-protected PDFs are not supported')
    if not 0 < len(reader.pages) <= MAX_PAGES:
        raise ValueError('Upload a PDF with 1 to 40 pages')
    pages, total = [], 0
    for number, page in enumerate(reader.pages, 1):
        content = page.get_contents()
        if content is not None and len(content.get_data()) > 5 * 1024 * 1024:
            raise ValueError('PDF page content is too large to process')
        text = (page.extract_text() or '').strip()
        total += len(text)
        if total > MAX_TEXT:
            raise ValueError('Paper exceeds 80,000 extracted characters; upload a shorter methods PDF')
        pages.append({'page': number, 'text': text})
    if total < 50:
        raise ValueError('No usable text found. Upload a text-based PDF; scanned images need OCR first')
    return pages


if __name__ == '__main__':
    try:
        pages = extract(sys.argv[1])
        Path(sys.argv[2]).write_text(json.dumps({'pages': pages}), encoding='utf-8')
    except Exception as error:
        Path(sys.argv[2]).write_text(json.dumps({'error': str(error)[:250]}), encoding='utf-8')
        sys.exit(1)
