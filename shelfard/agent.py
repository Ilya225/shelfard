"""
Shelfard interactive agent.

Wraps registry tools as Anthropic tool-use definitions and runs an interactive
REPL so the user can ask questions about their schemas in plain language.

Usage (via CLI):
    shelfard agent

Requires ANTHROPIC_API_KEY to be set in the environment.
"""

import json
import sys

import anthropic

from .models import ToolResult
from .registry import get_all_schemas, get_registered_schema


# ── Tool definitions ──────────────────────────────────────────────────────────

TOOLS = [
    {
        "name": "get_schema",
        "description": (
            "Retrieve the latest saved schema for a named data source from the registry. "
            "Returns column names, types, nullability, and the version timestamp."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "schema_name": {
                    "type": "string",
                    "description": "The name the schema was registered under.",
                },
            },
            "required": ["schema_name"],
        },
    },
    {
        "name": "get_all_schemas",
        "description": (
            "List all schemas stored in the registry with summary info: "
            "name, version count, latest version timestamp, source, and column count."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
]

SYSTEM_PROMPT = """\
You are the Shelfard schema assistant. Shelfard is a schema drift detection tool.
The registry stores versioned snapshots of data source schemas (REST APIs, databases).

You can call tools to read from the registry:
- get_all_schemas — list everything stored
- get_schema(schema_name) — inspect a specific schema in detail

Answer the user's questions about their schemas, help interpret drift, and suggest next steps.
Be concise. When showing schemas, highlight important fields (types, nullability, changes).
"""


# ── Tool dispatcher ───────────────────────────────────────────────────────────

def _execute_tool(name: str, tool_input: dict) -> dict:
    if name == "get_schema":
        result = get_registered_schema(tool_input["schema_name"])
    elif name == "get_all_schemas":
        result = get_all_schemas()
    else:
        result = ToolResult(success=False, error=f"Unknown tool: {name!r}")
    return result.to_dict()


# ── Interactive REPL ──────────────────────────────────────────────────────────

def run_agent() -> None:
    if not sys.stdin.isatty():
        print(
            "Error: 'shelfard agent' requires an interactive terminal.\n"
            "Run it in an activated shell:\n\n"
            "    conda activate shelfard\n"
            "    shelfard agent",
            file=sys.stderr,
        )
        sys.exit(1)

    client = anthropic.Anthropic()
    messages: list[dict] = []

    print("Shelfard Agent  (type 'exit' to quit)")
    print()

    while True:
        try:
            user_input = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if user_input.lower() in ("exit", "quit", "q"):
            break
        if not user_input:
            continue

        messages.append({"role": "user", "content": user_input})

        # Inner agentic loop — runs until the model stops calling tools
        while True:
            response = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=4096,
                system=SYSTEM_PROMPT,
                tools=TOOLS,
                messages=messages,
            )

            text_parts = [b.text for b in response.content if hasattr(b, "text") and b.text]
            tool_uses  = [b for b in response.content if b.type == "tool_use"]

            messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason == "end_turn":
                if text_parts:
                    print(f"Agent: {''.join(text_parts)}")
                    print()
                break

            # stop_reason == "tool_use" — print any prose before running tools
            if text_parts:
                print(f"Agent: {''.join(text_parts)}", flush=True)

            tool_results = []
            for block in tool_uses:
                result = _execute_tool(block.name, block.input)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result),
                })

            messages.append({"role": "user", "content": tool_results})
