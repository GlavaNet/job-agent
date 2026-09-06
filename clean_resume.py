#!/usr/bin/env python3
"""
Clean encoding issues from your resume without changing content.

Fixes:
  - Unusual unicode characters (private use chars, etc.)
  - Smart quotes → straight quotes
  - Em-dashes → hyphens
  - Tab characters → spaces
  - Excessive spacing

Preserves:
  - All actual text content
  - Structure and sections
  - Dates, names, job titles
  - Formatting (bullets, newlines)

Usage:
  python3 clean_resume.py --input ./resume/resume.pdf --output resume_clean.txt
  python3 clean_resume.py --input resume.pdf --output resume_clean.pdf
  python3 clean_resume.py --input resume.pdf  # Preview changes without saving
"""
import argparse
import logging
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


def setup_logging():
    """Configure logging."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s: %(message)s"
    )


def extract_text_from_pdf(pdf_path: str) -> str:
    """Extract text from PDF file."""
    try:
        import pdfplumber
        text = ""
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                text += page.extract_text() or ""
        return text
    except Exception as e:
        logger.error(f"Failed to extract text from PDF: {e}")
        return None


def extract_text_from_file(file_path: str) -> str:
    """Extract text from text file."""
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            return f.read()
    except Exception as e:
        logger.error(f"Failed to read {file_path}: {e}")
        return None


def load_resume(file_path: str) -> str:
    """Load resume from file (PDF or text)."""
    path = Path(file_path)
    
    if not path.exists():
        logger.error(f"File not found: {file_path}")
        return None
    
    logger.info(f"Loading resume from {file_path}...")
    
    if path.suffix.lower() == '.pdf':
        text = extract_text_from_pdf(file_path)
    else:
        text = extract_text_from_file(file_path)
    
    if text is None:
        return None
    
    logger.info(f"✓ Loaded {len(text)} characters")
    return text


def clean_encoding(text: str) -> tuple[str, list]:
    """
    Clean problematic characters from text.
    
    Returns:
        (cleaned_text, list_of_fixes_applied)
    """
    fixes = []
    original_len = len(text)
    
    # Remove unusual unicode characters (private use, control chars, etc.)
    # Keep common accented characters and Latin extended
    cleaned = ""
    for char in text:
        code_point = ord(char)
        
        # Allow common characters
        if code_point < 128:  # ASCII
            cleaned += char
        # Allow common accented characters (Latin-1 Supplement, Latin Extended)
        elif code_point in range(160, 383):  # À-ſ
            cleaned += char
        # Allow common symbols and punctuation
        elif code_point in range(8192, 8304):  # General Punctuation
            # But convert smart quotes and dashes
            if char == '"' or char == '"':  # Smart double quotes
                cleaned += '"'
                if '"' not in [c for c in cleaned[-5:]]:
                    fixes.append("Converted smart double quotes to straight quotes")
            elif char == ''' or char == ''':  # Smart single quotes
                cleaned += "'"
                if "'" not in [c for c in cleaned[-5:]]:
                    fixes.append("Converted smart single quotes to straight quotes")
            elif char == '–':  # En dash
                cleaned += '-'
                if 'en-dash' not in str(fixes):
                    fixes.append("Converted en-dashes to hyphens")
            elif char == '—':  # Em dash
                cleaned += '-'
                if 'em-dash' not in str(fixes):
                    fixes.append("Converted em-dashes to hyphens")
            else:
                cleaned += char
        # Allow other common symbols
        elif code_point >= 8304:
            # Skip most control/private use characters
            if code_point not in range(0xE000, 0xF900):  # Private use area
                cleaned += char
    
    # Replace tabs with spaces
    if '\t' in cleaned:
        cleaned = cleaned.replace('\t', ' ')
        fixes.append("Replaced tab characters with spaces")
    
    # Collapse multiple spaces to double spaces max
    import re
    cleaned = re.sub(r'   +', '  ', cleaned)
    
    cleaned_len = len(cleaned)
    if cleaned_len < original_len:
        chars_removed = original_len - cleaned_len
        fixes.append(f"Removed {chars_removed} problematic character(s)")
    
    return cleaned, fixes


def save_as_text(text: str, output_path: str) -> bool:
    """Save text to UTF-8 text file."""
    try:
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(text)
        logger.info(f"✓ Saved cleaned resume to {output_path}")
        return True
    except Exception as e:
        logger.error(f"Failed to save: {e}")
        return False


def save_as_pdf(text: str, output_path: str) -> bool:
    """Save text to PDF file."""
    try:
        from reportlab.lib.pagesizes import letter
        from reportlab.pdfgen import canvas
        from reportlab.lib.styles import ParagraphStyle
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
        from reportlab.lib.units import inch
        
        # Create PDF
        doc = SimpleDocTemplate(
            output_path,
            pagesize=letter,
            rightMargin=0.5*inch,
            leftMargin=0.5*inch,
            topMargin=0.5*inch,
            bottomMargin=0.5*inch,
        )
        
        # Create style
        style = ParagraphStyle(
            'Normal',
            fontName='Helvetica',
            fontSize=10,
            leading=12,
        )
        
        # Build PDF
        elements = []
        for line in text.split('\n'):
            if line.strip():
                elements.append(Paragraph(line, style))
            else:
                elements.append(Spacer(1, 0.1*inch))
        
        doc.build(elements)
        logger.info(f"✓ Saved cleaned resume to {output_path}")
        return True
    except ImportError:
        logger.warning("reportlab not installed. Cannot create PDF.")
        logger.info("Use --output file.txt to save as text instead.")
        return False
    except Exception as e:
        logger.error(f"Failed to save PDF: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(
        description="Clean encoding issues from resume without changing content"
    )
    parser.add_argument(
        "--input",
        required=True,
        help="Input resume file (PDF or TXT)",
    )
    parser.add_argument(
        "--output",
        help="Output file (if not specified, preview changes only)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview changes without saving",
    )
    args = parser.parse_args()
    
    setup_logging()
    
    # Load resume
    text = load_resume(args.input)
    if text is None:
        return 1
    
    # Clean encoding
    logger.info("")
    logger.info("=" * 70)
    logger.info("CLEANING ENCODING ISSUES")
    logger.info("=" * 70)
    logger.info("")
    
    cleaned_text, fixes = clean_encoding(text)
    
    if fixes:
        logger.info("Fixes applied:")
        for fix in fixes:
            logger.info(f"  • {fix}")
    else:
        logger.info("✓ No encoding issues found")
    
    # Preview changes
    if text != cleaned_text:
        logger.info("")
        logger.info("Sample of changes (first 500 chars):")
        logger.info("-" * 70)
        logger.info("BEFORE:")
        logger.info(repr(text[:500]))
        logger.info("")
        logger.info("AFTER:")
        logger.info(repr(cleaned_text[:500]))
        logger.info("-" * 70)
    
    # Save or preview
    if args.dry_run:
        logger.info("")
        logger.info("Dry run: no changes saved")
        return 0
    
    if not args.output:
        logger.info("")
        logger.info("To save cleaned resume, use --output:")
        logger.info(f"  python3 clean_resume.py --input {args.input} --output resume_clean.txt")
        return 0
    
    # Determine output format
    output_path = Path(args.output)
    if output_path.suffix.lower() == '.pdf':
        success = save_as_pdf(cleaned_text, args.output)
    else:
        success = save_as_text(cleaned_text, args.output)
    
    if success:
        logger.info("")
        logger.info("=" * 70)
        logger.info("DONE")
        logger.info("=" * 70)
        logger.info("")
        logger.info("Next steps:")
        logger.info(f"  1. Review {args.output} to verify it looks correct")
        logger.info("  2. If it looks good, replace your old resume with this cleaned version")
        logger.info("  3. Update RESUME_PATH in config.py to point to the new file")
        logger.info("  4. Wipe and regenerate your tailored resumes:")
        logger.info("       python3 wipe_generated_content.py --force")
        logger.info("       python3 auto_generate_resume_cover_letter.py --min-score 7")
        return 0
    else:
        return 1


if __name__ == "__main__":
    sys.exit(main())
