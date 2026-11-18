"""
PostgreSQL drift checker.

Loads a stored :class:`~shelfard.models.PostgresCheckerConfig`, resolves
``$VAR`` placeholders in the DSN and query at run time (never at storage time),
fetches the live schema via :class:`~shelfard.tools.postgres.reader.PostgresReader`,
diffs it against the registered baseline, and returns a :class:`ToolResult`.
"""

import os

from ..base import Checker
from ...models import PostgresCheckerConfig, ToolResult
from ...security import (
    check_pg_dsn_safe, pin_pg_dsn, private_hosts_allowed, query_template_error,
)


class PostgresChecker(Checker):
    """Run a stored PostgreSQL drift-check configuration."""

    def __init__(self, config: PostgresCheckerConfig, registry) -> None:
        self.config = config
        self.registry = registry

    def run(self) -> ToolResult:
        # 1. Validate that all required env vars are present
        missing = [name for name in self.config.env if name not in os.environ]
        if missing:
            return ToolResult(
                success=False,
                error=f"Missing required environment variables: {', '.join(missing)}",
                next_action_hint=(
                    "Export them before running. Inside 'shelfard agent', the agent forwards "
                    "env vars declared by checkers registered from the CLI; LLM API keys are "
                    "never forwarded."
                ),
            )

        # 1.2 — SQL queries must not contain {{template_vars}} (SQL-injection guard)
        if err := query_template_error(self.config.query):
            return ToolResult(success=False, error=err)

        # 1.5 — Resolve {{template_vars}} in the DSN (before $ENV_VAR substitution)
        dsn_template = self.registry.resolve_template(self.config.dsn)
        query_template = self.config.query

        # 2. Resolve $VAR placeholders in DSN and query (if any)
        resolved_dsn = _substitute(dsn_template, self.config.env)
        resolved_query = (
            _substitute(query_template, self.config.env)
            if query_template
            else None
        )

        # 2.5 — Checkers registered through MCP: the resolved DSN must target exactly one
        #       public host; pin the connection to the validated IP via hostaddr so libpq
        #       cannot resolve the name again (DNS-rebinding guard).
        if self.config.registered_via == "mcp" and not private_hosts_allowed():
            err, pinned_ip = check_pg_dsn_safe(resolved_dsn)
            if err:
                return ToolResult(success=False, error=f"Blocked unsafe DSN: {err}")
            if pinned_ip:
                resolved_dsn = pin_pg_dsn(resolved_dsn, pinned_ip)

        # 3. Fetch live schema
        from .reader import PostgresReader

        reader = PostgresReader(
            resolved_dsn,
            self.config.schema_name,
            table=self.config.table,
            query=resolved_query,
            db_schema=self.config.db_schema,
            sample_size=self.config.sample_size,
        )
        live_result = reader.get_schema()
        if not live_result.success:
            return ToolResult(
                success=False,
                error=f"Failed to fetch live schema: {live_result.error}",
            )

        # 4. Load baseline from registry
        baseline_result = self.registry.get_registered_schema(self.config.schema_name)
        if not baseline_result.success:
            return ToolResult(
                success=False,
                error=f"No baseline found for '{self.config.schema_name}'",
                next_action_hint=(
                    "Run 'shelfard postgres snapshot' first to capture a baseline"
                ),
            )

        # 5. Diff
        from ...schema_comparison import compare_schemas_from_dicts

        diff_result = compare_schemas_from_dicts(
            baseline_result.data["schema"],
            live_result.data["schema"],
        )
        if not diff_result.success:
            return ToolResult(success=False, error=diff_result.error)

        return ToolResult(
            success=True,
            data={
                "schema_name": self.config.schema_name,
                "baseline_version": baseline_result.data["version"],
                "baseline_version_number": baseline_result.data.get("version_number"),
                "diff": diff_result.data["diff"],
            },
        )


def _substitute(template: str, env_names: list[str]) -> str:
    """Replace every ``$NAME`` in *template* with the corresponding env var value."""
    for name in env_names:
        template = template.replace(f"${name}", os.environ.get(name, f"${name}"))
    return template
