"""The adapter generator writes Python from a spec, so the spec's author writes
Python onto someone's disk.

`_gw_load_spec` fetches specs over HTTPS, so "wrap https://vendor.example/openapi.json"
means whoever serves that URL influences the generated file. Values used to be
concatenated into source, and four of these vectors landed a live call in the
output while a fifth produced a file that would not parse. Everything that
reaches a *code* position now goes through repr(); everything that reaches a
docstring goes through _gw_docsafe().

Each test asserts on the parsed AST rather than on substrings: an escaped
payload still contains its own text, so only the tree can tell inert data from
executable code.
"""

import ast
import json

import pytest

from conftest import OPERATOR_TOKEN
import team_coordinator as tc

MARKER = "PWNED"
# Closes the string literal it lands in, then opens a fresh statement.
BREAKOUT = 'https://x.example.com").rstrip("/")\n' + MARKER + '()\nIGN = ("'


def _spec(server="https://x.example.com", path="/a", param="id",
          summary="does a thing", scheme_name=None):
    doc = {
        "openapi": "3.0.0",
        "info": {"title": "api", "version": "1"},
        "servers": [{"url": server}],
        "paths": {path: {"get": {"operationId": "op1", "summary": summary,
                                 "parameters": [{"name": param, "in": "path"}]}}},
    }
    if scheme_name:
        doc["components"] = {"securitySchemes":
                             {"K": {"type": "apiKey", "in": "header", "name": scheme_name}}}
    return json.dumps(doc)


def _generate(name, **kw):
    base = kw.pop("base_url", "")
    out = tc.gateway_generate_adapter(_spec(**kw), name=name, base_url=base,
                                      register=False, operator_token=OPERATOR_TOKEN)
    path = tc.ADAPTER_DIR / f"{name}.py"
    return out, (path.read_text() if path.exists() else None)


def _called_names(source):
    """Bare function names called anywhere in the generated module."""
    tree = ast.parse(source)
    return {n.func.id for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}


# --- The six injection vectors ---

def test_server_url_from_the_spec_cannot_inject():
    _, src = _generate("v1", server=BREAKOUT)
    assert MARKER not in _called_names(src)


def test_base_url_argument_cannot_inject():
    _, src = _generate("v2", base_url=BREAKOUT)
    assert MARKER not in _called_names(src)


def test_security_scheme_name_cannot_inject():
    _, src = _generate("v3", scheme_name=f'X"\n{MARKER}()\nY = "')
    assert MARKER not in _called_names(src)


def test_a_path_key_cannot_escape_the_docstring():
    _, src = _generate("v4", path=f'/a"""\n{MARKER}()\n"""')
    assert MARKER not in _called_names(src)


def test_a_path_parameter_name_cannot_inject():
    """Query and header params already used !r; only the path branch built a
    double-quoted literal by hand."""
    _, src = _generate("v5", param=f'id"); {MARKER}(); _p.replace("{{id')
    assert MARKER not in _called_names(src)


def test_a_summary_cannot_escape_the_docstring():
    _, src = _generate("v6", summary=f'x"""\n{MARKER}()\n"""')
    assert MARKER not in _called_names(src)


@pytest.mark.parametrize("payload", [
    'x\\', 'x"""y', "x\nnewline", "x\r\ny", 'x\'\'\'y', "x" * 400,
])
def test_hostile_summaries_still_produce_parseable_modules(payload):
    _, src = _generate("v7", summary=payload)
    ast.parse(src)          # raises if the docstring was escaped


# --- The output stays correct, not just safe ---

def test_a_normal_spec_still_generates_a_working_module():
    out, src = _generate("normal")
    assert "Generated MCP adapter" in out
    tree = ast.parse(src)
    funcs = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    assert "op1" in funcs and "_request" in funcs


def test_the_base_url_survives_verbatim_as_data():
    _, src = _generate("keeps", server="https://api.example.com/v2")
    tree = ast.parse(src)
    consts = {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant)
              and isinstance(n.value, str)}
    assert "https://api.example.com/v2" in consts


def test_a_hostile_value_survives_as_data_not_code():
    """It must still be *present* -- escaping is not the same as dropping it."""
    _, src = _generate("data", server=BREAKOUT)
    tree = ast.parse(src)
    consts = [n.value for n in ast.walk(tree)
              if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    assert any(MARKER in c for c in consts), "payload vanished instead of being escaped"
    assert MARKER not in _called_names(src)


def test_path_parameters_are_still_substituted():
    _, src = _generate("params", path="/pets/{petId}", param="petId")
    assert "_p.replace('{petId}'" in src or '_p.replace("{petId}"' in src


# --- The generated file has to actually run ---

def test_the_generated_adapter_imports_and_exposes_its_tools():
    """v8.3 taught the server to survive the mcp 1.x -> 2.x rename but never
    touched the file it writes, so every generated adapter was dead on 2.x. The
    suite passed throughout: nothing had ever executed the output."""
    import asyncio
    import importlib.util

    _, src = _generate("runnable")
    path = tc.ADAPTER_DIR / "runnable.py"
    spec = importlib.util.spec_from_file_location("generated_adapter", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    tools = asyncio.run(module.mcp.list_tools())
    assert [t.name for t in tools] == ["op1"]


def test_the_generated_import_covers_both_sdk_lines():
    _, src = _generate("shim")
    assert "mcp.server.fastmcp" in src and "mcp.server.mcpserver" in src


# --- The parse guard ---

def test_unparseable_output_is_refused_rather_than_written(monkeypatch):
    monkeypatch.setattr(tc, "_gw_gen_tool", lambda op, used: "def broken(:\n")
    out = tc.gateway_generate_adapter(_spec(), name="bad", register=False,
                                      operator_token=OPERATOR_TOKEN)
    assert "does not parse" in out
    assert not (tc.ADAPTER_DIR / "bad.py").exists()


def test_docsafe_neutralises_every_docstring_escape():
    for payload, banned in [('a"""b', '"""'), ("a\nb", "\n"), ("a\r\nb", "\r")]:
        assert banned not in tc._gw_docsafe(payload)
    assert not tc._gw_docsafe("trailing\\").endswith("\\")
    assert len(tc._gw_docsafe("x" * 900)) <= 280
