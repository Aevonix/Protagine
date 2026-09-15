"""Extraction formats package."""

from protagine.world_model.extraction.formats.text import TextExtractor
from protagine.world_model.extraction.formats.json_fmt import JSONExtractor
from protagine.world_model.extraction.formats.csv_fmt import CSVExtractor

# Optional extractors — load if dependencies are available
try:
    from protagine.world_model.extraction.formats.pdf import PDFExtractor
except ImportError:
    PDFExtractor = None

try:
    from protagine.world_model.extraction.formats.html_fmt import HTMLExtractor
except ImportError:
    HTMLExtractor = None

__all__ = [
    "TextExtractor",
    "JSONExtractor",
    "CSVExtractor",
    "PDFExtractor",
    "HTMLExtractor",
]
