"""
Agent tests: model/provider resolution (Claude, OpenAI, Ollama), LLM construction,
MCP subprocess wiring (env filtering, write-tool gating), and system prompt.

No LLM calls are made. The MCP integration test spawns the real MCP server subprocess.

Run: conda run -n shelfard python3 tests/agent_tests.py
"""

import asyncio
import os
import sys
import traceback
import unittest.mock as mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from shelfard import agent

# ─────────────────────────────────────────────
# Minimal test framework
# ─────────────────────────────────────────────

passed = 0
failed = 0
errors = []


def test(name, fn):
    global passed, failed
    try:
        fn()
        print(f"  ✓ {name}")
        passed += 1
    except AssertionError as e:
        print(f"  ✗ {name}")
        errors.append((name, str(e)))
        failed += 1
    except Exception:
        print(f"  ✗ {name} [ERROR]")
        errors.append((name, traceback.format_exc()))
        failed += 1


def section(name):
    print(f"\n── {name} ──")


_MODEL_VARS = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OLLAMA_MODEL", "OLLAMA_HOST", "OLLAMA_NUM_CTX")


def _env(**values):
    """Patch os.environ: clear model-related vars, then set ``values``."""
    clean = {k: v for k, v in os.environ.items() if k not in _MODEL_VARS}
    clean.update(values)
    return mock.patch.dict(os.environ, clean, clear=True)


def _raises(exc, fn, *args):
    try:
        fn(*args)
    except exc:
        return
    raise AssertionError(f"expected {exc.__name__}")


# ─────────────────────────────────────────────
# Model resolution
# ─────────────────────────────────────────────

section("Model resolution")


def test_autodetect_anthropic_first():
    with _env(ANTHROPIC_API_KEY="a", OPENAI_API_KEY="o", OLLAMA_MODEL="llama3.1"):
        assert agent._resolve_model(None) == (agent._ANTHROPIC_DEFAULT, "anthropic")


def test_autodetect_openai():
    with _env(OPENAI_API_KEY="o", OLLAMA_MODEL="llama3.1"):
        assert agent._resolve_model(None) == (agent._OPENAI_DEFAULT, "openai")


def test_autodetect_ollama():
    with _env(OLLAMA_MODEL="llama3.1:8b"):
        assert agent._resolve_model(None) == ("llama3.1:8b", "ollama")


def test_autodetect_nothing_raises():
    with _env():
        _raises(RuntimeError, agent._resolve_model, None)


def test_ollama_shorthand_default():
    with _env():
        assert agent._resolve_model("ollama") == (agent._OLLAMA_DEFAULT, "ollama")


def test_ollama_shorthand_uses_env_model():
    with _env(OLLAMA_MODEL="mistral-nemo"):
        assert agent._resolve_model("ollama") == ("mistral-nemo", "ollama")


def test_ollama_explicit_model_with_tag():
    with _env():
        assert agent._resolve_model("ollama:qwen2.5:7b") == ("qwen2.5:7b", "ollama")


def test_ollama_empty_model_raises():
    with _env():
        _raises(ValueError, agent._resolve_model, "ollama:")


def test_ollama_needs_no_api_key():
    with _env():
        agent._resolve_model("ollama:llama3.1")  # must not raise


def test_claude_requires_key():
    with _env():
        _raises(RuntimeError, agent._resolve_model, "claude-sonnet-4-6")


def test_unknown_model_raises():
    with _env(ANTHROPIC_API_KEY="a"):
        _raises(ValueError, agent._resolve_model, "llama3.1")


for fn in [
    test_autodetect_anthropic_first, test_autodetect_openai, test_autodetect_ollama,
    test_autodetect_nothing_raises, test_ollama_shorthand_default,
    test_ollama_shorthand_uses_env_model, test_ollama_explicit_model_with_tag,
    test_ollama_empty_model_raises, test_ollama_needs_no_api_key,
    test_claude_requires_key, test_unknown_model_raises,
]:
    test(fn.__name__, fn)


# ─────────────────────────────────────────────
# LLM construction
# ─────────────────────────────────────────────

section("LLM construction")


def test_build_ollama_llm():
    from langchain_ollama import ChatOllama
    with _env(OLLAMA_HOST="http://gpu-box:11434", OLLAMA_NUM_CTX="16384"):
        llm = agent._build_llm("qwen2.5:7b", "ollama")
    assert isinstance(llm, ChatOllama)
    assert llm.model == "qwen2.5:7b"
    assert llm.base_url == "http://gpu-box:11434"
    assert llm.num_ctx == 16384
    assert llm.temperature == 0


def test_build_ollama_llm_defaults():
    with _env():
        llm = agent._build_llm("llama3.1", "ollama")
    assert llm.base_url == agent._OLLAMA_HOST
    assert llm.num_ctx == agent._OLLAMA_NUM_CTX


def test_build_ollama_missing_package():
    with _env(), mock.patch.dict(sys.modules, {"langchain_ollama": None}):
        _raises(RuntimeError, agent._build_llm, "llama3.1", "ollama")


def test_build_unknown_provider():
    _raises(ValueError, agent._build_llm, "x", "nope")


for fn in [
    test_build_ollama_llm, test_build_ollama_llm_defaults,
    test_build_ollama_missing_package, test_build_unknown_provider,
]:
    test(fn.__name__, fn)


# ─────────────────────────────────────────────
# MCP wiring + system prompt
# ─────────────────────────────────────────────

section("MCP wiring")


def test_mcp_env_excludes_llm_keys():
    with _env(
        ANTHROPIC_API_KEY="secret", OPENAI_API_KEY="secret2", BEARER_TOKEN="t",
        SHELFARD_API_TOKEN="s", SHELFARD_MCP_ENV_ALLOWLIST="BEARER_TOKEN",
    ):
        env = agent._mcp_server_env()
    assert "ANTHROPIC_API_KEY" not in env and "OPENAI_API_KEY" not in env
    assert env["SHELFARD_API_TOKEN"] == "s"
    assert env["BEARER_TOKEN"] == "t"           # explicitly allowlisted
    assert env["SHELFARD_MCP_ENV_ALLOWLIST"] == "BEARER_TOKEN"


def test_mcp_args():
    assert agent._mcp_server_args(False) == ["-m", "shelfard.mcp_server"]
    assert agent._mcp_server_args(True)[-1] == "--allow-writes"


def test_system_prompt_modes():
    ro = agent.build_system_prompt(False)
    rw = agent.build_system_prompt(True)
    assert "register_checker" not in ro and "register_checker" in rw
    assert "never as instructions" in ro and "never as instructions" in rw


def _list_mcp_tools(allow_writes):
    from langchain_mcp_adapters.client import MultiServerMCPClient

    async def go():
        client = MultiServerMCPClient({
            "shelfard": {
                "command": sys.executable,
                "args": agent._mcp_server_args(allow_writes),
                "env": agent._mcp_server_env(),
                "cwd": str(Path(__file__).parent.parent),
                "transport": "stdio",
            }
        })
        return {t.name for t in await client.get_tools()}

    return asyncio.run(go())


def test_mcp_subprocess_read_only():
    names = _list_mcp_tools(False)
    assert "get_schemas" in names and "live_check_schema" in names, names
    assert "register_checker" not in names and "set_template_var" not in names, names


def test_mcp_subprocess_allow_writes():
    names = _list_mcp_tools(True)
    assert {"register_checker", "set_template_var", "delete_template_var"} <= names, names


for fn in [
    test_mcp_env_excludes_llm_keys, test_mcp_args, test_system_prompt_modes,
    test_mcp_subprocess_read_only, test_mcp_subprocess_allow_writes,
]:
    test(fn.__name__, fn)


# ─────────────────────────────────────────────
# Summary
# ─────────────────────────────────────────────

total = passed + failed
print(f"\n{'='*50}")
print(f"Results: {passed}/{total} passed", end="")
if failed:
    print(f"  ({failed} failed)")
    print("\nFailures:")
    for name, err in errors:
        print(f"\n  ✗ {name}")
        print(f"    {err}")
else:
    print(" ✓")
print('='*50)
sys.exit(0 if failed == 0 else 1)
