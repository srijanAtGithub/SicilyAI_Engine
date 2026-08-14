import dataclasses
import pytest


# tool_manager.py — structural checks (lightweight import, no API calls
# are made — ToolManager() is never instantiated, only the class itself
# is inspected, since instantiating it creates live OpenAI/Chat clients)
try:
    from Agent.tool_manager import ToolManager, ToolEntry
    _TOOL_MANAGER_IMPORT_ERROR = None
except Exception as e:  # pragma: no cover
    ToolManager, ToolEntry = None, None
    _TOOL_MANAGER_IMPORT_ERROR = e


def test_tool_manager_imports_cleanly():
    assert _TOOL_MANAGER_IMPORT_ERROR is None, f"tool_manager.py failed to import: {_TOOL_MANAGER_IMPORT_ERROR}"


@pytest.mark.skipif(ToolManager is None, reason="tool_manager import failed — see test_tool_manager_imports_cleanly")
def test_tool_manager_has_expected_interface():
    for attr in ("register", "unregister", "_generate_server_description", "get_tools_for_servers", "loaded_servers", "all_tools"):
        assert hasattr(ToolManager, attr), f"ToolManager is missing expected attribute/method: {attr}"


@pytest.mark.skipif(ToolEntry is None, reason="tool_manager import failed — see test_tool_manager_imports_cleanly")
def test_tool_entry_dataclass_fields():
    field_names = {f.name for f in dataclasses.fields(ToolEntry)}
    assert field_names == {"tool", "server", "embedding", 'tokens'}
