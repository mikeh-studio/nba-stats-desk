import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent.legacy_types import ROUTE_TOOLS
from app.agent.tools import get_tool_schemas


def test_route_tools_are_registered_tool_names() -> None:
    registered_tool_names = {schema["name"] for schema in get_tool_schemas()}

    for tool_names in ROUTE_TOOLS.values():
        for tool_name in tool_names:
            assert tool_name in registered_tool_names
