"""Unified tool adapter layer — единый интерфейс для всех инструментов анализа."""
from .base import AnalysisOptions, AnalysisResult, Finding, FindingKind, Location, ToolAdapter
from .registry import ToolRegistry, detect_language, detect_config_type

__all__ = [
    "AnalysisOptions",
    "AnalysisResult",
    "Finding",
    "FindingKind",
    "Location",
    "ToolAdapter",
    "ToolRegistry",
    "detect_language",
    "detect_config_type",
]
