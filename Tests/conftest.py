import ast
import sys
from pathlib import Path

# Paths — adjust here if your folder layout differs
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

CONNECTORS_PATH        = PROJECT_ROOT / "Agent/connectors.py"
TELEGRAM_COMMANDS_PATH = PROJECT_ROOT / "Agent/telegram_commands.py"
SETTINGS_EXAMPLE_PATH  = PROJECT_ROOT / "settings.example.json"
AGENT_PATH = PROJECT_ROOT / "Agent/agent.py"
SOULS_DIR  = PROJECT_ROOT / "Souls"

# AST helpers — read source as text, no project imports required
def parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def get_function_node(tree: ast.Module, func_name: str):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
            return node
    return None


def all_function_names(tree: ast.Module) -> set[str]:
    return {
        node.name for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def get_dict_literal(tree: ast.Module, var_name: str):
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == var_name and isinstance(node.value, ast.Dict):
                    return node.value
    return None


def dict_str_keys(dict_node: ast.Dict) -> list[str]:
    return [k.value for k in dict_node.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)]


def extract_call_first_arg_strings(scope_node, call_name: str) -> list[str]:
    """Within a given AST node, find all calls to `call_name` and return the
    first positional argument when it's a string literal."""
    results = []
    for node in ast.walk(scope_node):
        if isinstance(node, ast.Call):
            fn = node.func
            name = fn.id if isinstance(fn, ast.Name) else (fn.attr if isinstance(fn, ast.Attribute) else None)
            if name == call_name and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                results.append(node.args[0].value)
    return results
