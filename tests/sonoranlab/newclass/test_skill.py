"""The /new-class skill names only allowed, existing MCP tools (static: no server, no agent)."""

import re
from pathlib import Path

import yaml
from newclass_agent import ALLOWED

from sonoranlab.api import call

SKILL = Path(__file__).resolve().parents[3] / ".claude/skills/new-class/SKILL.md"
FORBIDDEN_MENTIONS = {"pose_frame", "animate", "retry_job", "cancel_job"}  # named only in the "never call" rule


def sections() -> tuple[dict, str]:
    text = SKILL.read_text()
    _, front, body = text.split("---\n", 2)
    return yaml.safe_load(front), body


def test_frontmatter_and_tool_list_match_the_allowlist():
    front, body = sections()
    assert front["name"] == "new-class" and front["description"]
    assert {t.strip() for t in front["allowed-tools"].split(",")} == {f"mcp__sonoranlab__{t}" for t in ALLOWED}
    listed = body.split("## Tools you may call (nothing else)", 1)[1].split("\n## ", 1)[0]
    assert set(re.findall(r"`([a-z_]+)`", listed)) == ALLOWED
    assert len(SKILL.read_text().splitlines()) <= 250


def test_every_tool_named_is_allowed_and_exists():
    _, body = sections()
    tools = {t["name"] for t in call("capabilities", {})["result"]["tools"]}
    assert tools >= ALLOWED  # every allowed tool is discoverable in the default (editor-less) server
    named = {name for name in re.findall(r"`([a-z_]+)(?:\(|`)", body) if name in tools | FORBIDDEN_MENTIONS}
    assert named - ALLOWED <= FORBIDDEN_MENTIONS
    never = body.split("2. Never run", 1)[1].split("\n3. ", 1)[0]
    for name in FORBIDDEN_MENTIONS:
        assert f"`{name}`" in never and name not in ALLOWED
    assert not {n for n in tools if re.search("approve|install|review_record", n)}
