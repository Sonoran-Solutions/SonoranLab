"""Reuse the Phase 3.5 fixtures and fake editor without changing their tests."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "repair"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mcp"))
