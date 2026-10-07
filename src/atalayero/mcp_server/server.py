"""The MCP server of the investigator's tools (ADR-0016): thin wrappers over
`atalayero.agent.tools`, served over stdio.

Run it with `python -m atalayero.mcp_server`. It serves the alerts of the dev and golden sets, and
never their answers.
"""

from collections.abc import Callable
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from atalayero.agent.tools import MAX_DAYS, MAX_TRANSACTIONS, ToolBox

INSTRUCTIONS = (
    "Read-only tools to investigate one alert of the Atalayero transaction monitor. An alert is "
    "an account-day: everything one account sent and received on one day. Every tool takes the "
    "alert ID and sees only data from before the end of the alert's day. Amounts are in USD as "
    "paid. Account keys are 'bank:account'."
)


def _call(tool: Callable[..., dict[str, Any]], *args: Any) -> dict[str, Any]:  # noqa: ANN401
    """Run a tool; a bad request (such as an unknown alert) reaches the model as its message."""
    try:
        return tool(*args)
    except ValueError as error:
        raise ToolError(str(error)) from error


def build_server(toolbox: ToolBox) -> MCPServer:
    server = MCPServer(name="atalayero", instructions=INSTRUCTIONS)

    @server.tool()
    def get_account_profile(alert_id: str, account_key: str | None = None) -> dict[str, Any]:
        """An account's activity: what it sent and received over its whole history and on the
        alert's day (count, USD, distinct counterparties), when it was first and last seen, its
        self-transfers, payment formats and currencies. Defaults to the alerted account."""
        return _call(toolbox.account_profile, alert_id, account_key)

    @server.tool(
        description=(
            "An account's transactions in the last `days` (1 to "
            f"{MAX_DAYS}) before the end of the alert's day, newest first, up to `limit` (at most "
            f"{MAX_TRANSACTIONS}); the sent and received totals cover all of them. Each row gives "
            "the transaction ID, time, direction (out or in), counterparty, USD amount, native "
            "amount and currency, and payment format. Defaults to the alerted account."
        )
    )
    def get_transactions(
        alert_id: str, account_key: str | None = None, days: int = 1, limit: int = 50
    ) -> dict[str, Any]:
        return _call(toolbox.transactions, alert_id, account_key, days, limit)

    @server.tool()
    def get_graph_neighborhood(
        alert_id: str, account_key: str | None = None, hops: int = 1, days: int = 3
    ) -> dict[str, Any]:
        """An account's counterparties in the last `days`, with the money sent to and received
        from each, and how many accounts each counterparty pays and is paid by in that time (a
        counterparty that pays many accounts is the hub of a fan-out); with hops=2, also the
        accounts those counterparties deal with. Lists cycles that bring money back to the
        account in time order (A→B→A, A→B→C→A), with their transaction IDs. Defaults to the
        alerted account."""
        return _call(toolbox.neighbourhood, alert_id, account_key, hops, days)

    @server.tool()
    def explain_score(alert_id: str) -> dict[str, Any]:
        """Why the model scored the alert's account-day as it did: its top-scored transactions,
        each with the features that pushed its score up (positive log-odds) or down the most."""
        return _call(toolbox.explain_score, alert_id)

    @server.tool()
    def search_typologies(query: str, k: int = 4) -> dict[str, Any]:
        """Search the typology notes by meaning: describe what you see (for example "money
        returns to the sender through two intermediaries within a day") and get the closest
        sections, each with the typology it belongs to. Notes cover the eight typologies, laundering
        without a clear typology (`unclassified`) and legitimate activity that looks suspicious
        (`none`)."""
        return _call(toolbox.search_typologies, query, k)

    return server


def main() -> None:
    from atalayero.evals.golden import load_alerts
    from atalayero.settings import Settings

    settings = Settings()
    alerts = [*load_alerts(settings, "dev"), *load_alerts(settings, "golden")]
    build_server(ToolBox(settings, alerts)).run("stdio")
