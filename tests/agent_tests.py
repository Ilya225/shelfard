"""
Agent tests: model/provider resolution (Claude, OpenAI, Ollama), LLM construction,
MCP subprocess wiring (env filtering, write-tool gating), and system prompt.

No LLM calls are made. The MCP integration test spawns the real MCP server subprocess.

Run: conda run -n shelfard python3 tests/agent_tests.py
"""

import asyncio
import importlib.util
import os
import sys
import tempfile
import traceback
import unittest.mock as mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from shelfard import agent
from shelfard import registry
from shelfard.models import RestProbeConfig

HAS_OLLAMA = importlib.util.find_spec("langchain_ollama") is not None

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


_MODEL_VARS = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OLLAMA_MODEL", "OLLAMA_HOST",
               "OLLAMA_NUM_CTX", "SHELFARD_DEFAULT_MODEL")


def _env(**values):
    """Patch os.environ: clear model-related vars, then set ``values``."""
    clean = {k: v for k, v in os.environ.items() if k not in _MODEL_VARS}
    clean.update(values)
    return mock.patch.dict(os.environ, clean, clear=True)


class _isolated_default_registry:
    """Point the module-level default registry at a temp dir."""

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = registry._default._root
        registry._default._root = Path(self._tmp.name)
        return Path(self._tmp.name)

    def __exit__(self, *exc):
        registry._default._root = self._orig
        self._tmp.cleanup()


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


def test_default_model_env_override():
    with _env(ANTHROPIC_API_KEY="a", SHELFARD_DEFAULT_MODEL="ollama:llama3.1"):
        assert agent._resolve_model(None) == ("llama3.1", "ollama")
    with _env(OPENAI_API_KEY="o", SHELFARD_DEFAULT_MODEL="gpt-5-mini"):
        assert agent._resolve_model(None) == ("gpt-5-mini", "openai")


def test_flag_beats_default_model_env():
    with _env(SHELFARD_DEFAULT_MODEL="ollama:llama3.1"):
        assert agent._resolve_model("ollama:qwen3") == ("qwen3", "ollama")


def test_current_default_models():
    with _env(ANTHROPIC_API_KEY="a"):
        assert agent._resolve_model("anthropic") == ("claude-sonnet-5-5", "anthropic")
    with _env(OPENAI_API_KEY="o"):
        assert agent._resolve_model("openai") == ("gpt-5", "openai")


for fn in [
    test_autodetect_anthropic_first, test_autodetect_openai, test_autodetect_ollama,
    test_autodetect_nothing_raises, test_ollama_shorthand_default,
    test_ollama_shorthand_uses_env_model, test_ollama_explicit_model_with_tag,
    test_ollama_empty_model_raises, test_ollama_needs_no_api_key,
    test_claude_requires_key, test_unknown_model_raises, test_default_model_env_override,
    test_flag_beats_default_model_env, test_current_default_models,
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
    if not HAS_OLLAMA and fn is not test_build_unknown_provider:
        print(f"  - {fn.__name__} (skipped: langchain-ollama not installed)")
        continue
    test(fn.__name__, fn)


# ─────────────────────────────────────────────
# MCP wiring + system prompt
# ─────────────────────────────────────────────

section("MCP wiring")


def test_mcp_env_excludes_llm_keys():
    with _isolated_default_registry(), _env(
        ANTHROPIC_API_KEY="secret", OPENAI_API_KEY="secret2", BEARER_TOKEN="t",
        SHELFARD_API_TOKEN="s", SHELFARD_MCP_ENV_ALLOWLIST="BEARER_TOKEN",
    ):
        env = agent._mcp_server_env()
    assert "ANTHROPIC_API_KEY" not in env and "OPENAI_API_KEY" not in env
    assert env["SHELFARD_API_TOKEN"] == "s"
    assert env["BEARER_TOKEN"] == "t"           # explicitly allowlisted
    assert env["SHELFARD_MCP_ENV_ALLOWLIST"] == "BEARER_TOKEN"


def test_mcp_env_forwards_cli_probe_vars():
    """AGT-1: env vars declared by CLI-registered probes reach the MCP subprocess."""
    with _isolated_default_registry(), _env(
        BEARER_TOKEN="t", MCP_ONLY_TOKEN="m", UNRELATED="u", ANTHROPIC_API_KEY="secret",
    ):
        registry.register_probe("cli_api", RestProbeConfig(
            schema_name="cli_api", url="https://x", headers=[],
            env=["BEARER_TOKEN", "ANTHROPIC_API_KEY"],
        ))
        registry.register_probe("mcp_api", RestProbeConfig(
            schema_name="mcp_api", url="https://y", headers=[], env=["MCP_ONLY_TOKEN"],
            registered_via="mcp",
        ))
        env = agent._mcp_server_env()
    assert env.get("BEARER_TOKEN") == "t"
    assert "MCP_ONLY_TOKEN" not in env          # only CLI-registered probes are trusted
    assert "UNRELATED" not in env
    assert "ANTHROPIC_API_KEY" not in env       # never forwarded, even if declared


def test_mcp_args():
    assert agent._mcp_server_args(False) == ["-m", "shelfard.mcp_server"]
    assert agent._mcp_server_args(True)[-1] == "--allow-writes"


def test_system_prompt_modes():
    ro = agent.build_system_prompt(False)
    rw = agent.build_system_prompt(True)
    assert "register_probe" not in ro and "register_probe" in rw
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
    assert "register_probe" not in names and "set_template_var" not in names, names


def test_mcp_subprocess_allow_writes():
    names = _list_mcp_tools(True)
    assert set(agent.WRITE_TOOLS) <= names, names


for fn in [
    test_mcp_env_excludes_llm_keys, test_mcp_env_forwards_cli_probe_vars,
    test_mcp_args, test_system_prompt_modes,
    test_mcp_subprocess_read_only, test_mcp_subprocess_allow_writes,
]:
    test(fn.__name__, fn)


# ─────────────────────────────────────────────
# Write-tool approval (SEC-3) + memory bounds (AGT-4)
# ─────────────────────────────────────────────

section("Approval + memory")

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel  # noqa: E402
from langchain_core.messages import AIMessage  # noqa: E402
from langchain_core.tools import tool  # noqa: E402

_tool_calls = []


@tool
def set_template_var(name: str, value: str) -> str:
    """Store a template variable."""
    _tool_calls.append(("set_template_var", name, value))
    return "stored"


@tool
def get_schemas() -> str:
    """List schemas."""
    _tool_calls.append(("get_schemas",))
    return "[]"


class _FakeModel(GenericFakeChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


def _fake_llm(tool_name, args):
    return _FakeModel(messages=iter([
        AIMessage(content="", tool_calls=[{"name": tool_name, "args": args, "id": "call_1"}]),
        AIMessage(content="done"),
    ]))


def _turn(llm, tools, allow_writes, answer):
    _tool_calls.clear()
    prompts = []

    def ask(prompt):
        prompts.append(prompt)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    built = agent.build_agent(llm, tools, allow_writes=allow_writes)
    state = asyncio.run(agent.run_turn(built, "go", agent._new_config(), ask=ask))
    return state, prompts


def test_write_tool_runs_after_approval():
    state, prompts = _turn(_fake_llm("set_template_var", {"name": "h", "value": "x"}),
                           [set_template_var], True, "y")
    assert len(prompts) == 1
    assert _tool_calls == [("set_template_var", "h", "x")]
    assert state["messages"][-1].content == "done"


def test_write_tool_blocked_when_rejected():
    state, prompts = _turn(_fake_llm("set_template_var", {"name": "h", "value": "x"}),
                           [set_template_var], True, "n")
    assert len(prompts) == 1 and _tool_calls == []
    tool_msgs = [m.content for m in state["messages"] if m.type == "tool"]
    assert tool_msgs and "rejected" in tool_msgs[0]


def test_write_tool_blocked_on_ctrl_c():
    _, prompts = _turn(_fake_llm("set_template_var", {"name": "h", "value": "x"}),
                       [set_template_var], True, KeyboardInterrupt())
    assert len(prompts) == 1 and _tool_calls == []


def test_read_tool_needs_no_approval():
    _, prompts = _turn(_fake_llm("get_schemas", {}), [get_schemas], True, "n")
    assert prompts == [] and _tool_calls == [("get_schemas",)]


def test_middleware_configuration():
    from langchain.agents.middleware import HumanInTheLoopMiddleware, SummarizationMiddleware
    llm = _fake_llm("get_schemas", {})
    with mock.patch.object(agent, "create_agent") as ca:
        agent.build_agent(llm, [], allow_writes=False)
        mw = ca.call_args.kwargs["middleware"]
        assert [type(m) for m in mw] == [SummarizationMiddleware]
        agent.build_agent(llm, [], allow_writes=True)
        mw = ca.call_args.kwargs["middleware"]
        assert [type(m) for m in mw] == [SummarizationMiddleware, HumanInTheLoopMiddleware]


def test_summary_trigger_scales_with_ollama_context():
    with _env(OLLAMA_NUM_CTX="4096"):
        assert agent._summary_token_trigger("ollama") == int(4096 * 0.7)
    assert agent._summary_token_trigger("anthropic") == agent._SUMMARY_TRIGGER_TOKENS


def test_new_config_is_fresh_thread():
    a, b = agent._new_config(), agent._new_config()
    assert a["configurable"]["thread_id"] != b["configurable"]["thread_id"]


for fn in [
    test_write_tool_runs_after_approval, test_write_tool_blocked_when_rejected,
    test_write_tool_blocked_on_ctrl_c, test_read_tool_needs_no_approval,
    test_middleware_configuration, test_summary_trigger_scales_with_ollama_context,
    test_new_config_is_fresh_thread,
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
