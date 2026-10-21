"""Reuse Phase 3 CPU fixtures and the DYL-113 stdio harness."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mcp"))
