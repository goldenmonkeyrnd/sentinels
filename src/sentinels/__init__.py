"""Inspect declared integration requirements without executing application code."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from .models import AnalysisReport, Bundle, Config

if TYPE_CHECKING:
    from collections.abc import Mapping

__version__ = "0.1.0"
__all__ = ["AnalysisReport", "Bundle", "Config", "analyze_sources", "analyze_project"]


def analyze_sources(files: Mapping[str, str], config: Config | dict[str, object]) -> AnalysisReport:
    from .analyzer import analyze_sources as analyze

    return analyze(files, config)


def analyze_project(root: str | Path, config: Config | dict[str, object]) -> AnalysisReport:
    from .analyzer import analyze_project as analyze

    return analyze(root, config)
