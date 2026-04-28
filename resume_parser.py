# resume_parser.py
import logging
import os
from collections import defaultdict

import fitz  # pymupdf

logger = logging.getLogger(__name__)


def load_resume(path: str) -> str:
    """
    Extract plain text from a PDF résumé using word-level,
    coordinate-aware extraction.

    Words are grouped by their vertical midpoint (snapped to a 2 pt
    grid) so that left-aligned job titles and right-aligned dates on
    the same visual line are reconstructed correctly on one text line.

    Raises FileNotFoundError with a helpful message if the path does
    not exist, rather than letting fitz produce an opaque error.
    """
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"Resume not found at '{path}'. "
            "Check RESUME_PATH in config.py and ensure the file exists."
        )

    doc = fitz.open(path)
    all_lines: list[str] = []

    for page in doc:
        # Each word tuple: (x0, y0, x1, y1, word, block_no, line_no, word_no)
        words = page.get_text("words")
        if not words:
            continue

        # Group words by vertical midpoint rounded to nearest 2 pts
        buckets: defaultdict[int, list[tuple[float, str]]] = defaultdict(list)
        for x0, y0, x1, y1, text, *_ in words:
            mid_y = round((y0 + y1) / 2 / 2) * 2
            buckets[mid_y].append((x0, text))

        for y_key in sorted(buckets):
            line_text = " ".join(
                word for _, word in sorted(buckets[y_key], key=lambda w: w[0])
            )
            all_lines.append(line_text)

        all_lines.append("")  # blank line between pages

    doc.close()

    # Collapse runs of more than 2 consecutive blank lines
    cleaned: list[str] = []
    blank_count = 0
    for line in all_lines:
        if line.strip() == "":
            blank_count += 1
            if blank_count <= 2:
                cleaned.append("")
        else:
            blank_count = 0
            cleaned.append(line)

    result = "\n".join(cleaned).strip()
    logger.debug("Extracted %d characters from %s", len(result), path)

    if len(result) < 100:
        logger.warning(
            "Resume extraction produced only %d characters from '%s'. "
            "The PDF may be image-based or encrypted.",
            len(result), path,
        )

    return result
