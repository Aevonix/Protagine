"""Native tool handlers for Protagine's reasoning loop.

These tools run inside Protagine without requiring a host harness roundtrip.
"""

from protagine.reasoning.native_tools.calculate import CalculateTool
from protagine.reasoning.native_tools.web_search import WebSearchTool
from protagine.reasoning.native_tools.file_ops import ReadFileTool, WriteFileTool, ListDirectoryTool

__all__ = [
    "CalculateTool",
    "WebSearchTool",
    "ReadFileTool",
    "WriteFileTool",
    "ListDirectoryTool",
]
