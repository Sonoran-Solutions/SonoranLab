"""Make the existing Phase 2/3 review fixtures and stdio harness reusable in this subtree."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mcp"))
