"""
Health Agent tools — connection monitoring, credential refresh, status reporting.
"""

import logging
import os
from datetime import datetime, timezone

from google.adk.tools.tool_context import ToolContext

try:
    from ..agent_registry.registry import AgentRegistry
    from ..connectors import get_connector
except ImportError:
    from databot_agent.agent_registry.registry import AgentRegistry
    from databot_agent.connectors import get_connector

logger = logging.getLogger(__name__)

# Source types that have their own dedicated health-check pipelines and should
# not be probed by the generic connection test.
_BUILTIN_SOURCE_TYPES = {"tableau", "bigquery"}


def check_all_connections(tool_context: ToolContext) -> dict:
    """
    Iterate over every active agent in the registry and test its connector.

    Built-in source types (tableau, bigquery) are skipped because they have
    their own health-check logic outside this agent's scope.

    Returns a summary dict with passed, failed, skipped lists and a timestamp.
    """
    registry = AgentRegistry.get()
    agents = registry.list_agents()  # {source_type: metadata_dict}

    passed = []
    failed = []
    skipped = []

    for stype, agent_meta in agents.items():
        status: str = agent_meta.get("status", "")

        if status != "active":
            continue

        if stype in _BUILTIN_SOURCE_TYPES:
            skipped.append(stype)
            continue

        connector_cls = get_connector(stype)
        if connector_cls is None:
            skipped.append(stype)
            continue

        # Build credentials from env vars declared in the registry entry
        cred_env_keys: list[str] = agent_meta.get("credentials_env_keys", [])
        credentials = {key: os.getenv(key, "") for key in cred_env_keys}

        try:
            result: dict = connector_cls.test_connection(credentials)
            success = result.get("status") == "success"
        except Exception as exc:  # noqa: BLE001
            logger.exception("test_connection raised for source_type=%s", stype)
            result = {"status": "error", "message": str(exc)}
            success = False

        try:
            registry.update_last_tested(stype, success=success)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not update last_tested for %s: %s", stype, exc)

        if success:
            passed.append(stype)
        else:
            failed.append({"source_type": stype, "error": result.get("message", str(result))})

    return {
        "checked": len(passed) + len(failed),
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def check_source_connection(tool_context: ToolContext, source_type: str) -> dict:
    """
    Test the connection for a single source type and update the registry.

    Credentials are resolved from the environment using the keys declared in
    the registry entry.  The raw credential values are never returned.

    On failure the error message is stored in tool_context.state so that the
    health agent can surface it to the caller.
    """
    source_type = source_type.strip().lower().replace("-", "_")

    registry = AgentRegistry.get()
    agents = registry.list_agents()  # {source_type: metadata_dict}

    agent_meta: dict | None = agents.get(source_type)

    if agent_meta is None:
        return {
            "source_type": source_type,
            "status": "error",
            "message": f"No registry entry found for source_type '{source_type}'.",
            "credentials_present": False,
            "last_tested": None,
        }

    connector_cls = get_connector(source_type)
    if connector_cls is None:
        return {
            "source_type": source_type,
            "status": "error",
            "message": f"No connector found for source_type '{source_type}'.",
            "credentials_present": False,
            "last_tested": agent_meta.get("last_tested"),
        }

    # Build and validate credentials
    cred_env_keys: list[str] = agent_meta.get("credentials_env_keys", [])
    credentials = {key: os.getenv(key, "") for key in cred_env_keys}
    credentials_present = all(os.getenv(key) for key in cred_env_keys)

    # Run the connection test
    try:
        result: dict = connector_cls.test_connection(credentials)
        success = result.get("status") == "success"
        message = result.get("message", "")
    except Exception as exc:  # noqa: BLE001
        logger.exception("test_connection raised for source_type=%s", source_type)
        success = False
        message = str(exc)

    # Persist result to registry
    try:
        registry.update_last_tested(source_type, success=success)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not update last_tested for %s: %s", source_type, exc)

    # Store health error in state for downstream agents
    if not success:
        tool_context.state[f"{source_type}_health_error"] = message

    # Refresh last_tested from registry (may have been updated above)
    updated_meta = registry.list_agents().get(source_type)
    last_tested = (updated_meta or agent_meta).get("last_tested")

    return {
        "source_type": source_type,
        "status": "success" if success else "error",
        "message": message,
        "credentials_present": credentials_present,
        "last_tested": last_tested,
    }


def get_health_report(tool_context: ToolContext) -> dict:
    """
    Return a full health snapshot for every agent in the registry.

    For each agent, report whether its credential env vars are currently
    populated (True/False per key) without exposing actual values.

    The summary field is a human-readable one-liner.
    """
    registry = AgentRegistry.get()
    agents = registry.list_agents()  # {source_type: metadata_dict}

    agent_statuses = []
    total = 0
    active_count = 0
    failing_count = 0

    for stype, meta in agents.items():
        total += 1
        status: str = meta.get("status", "unknown")
        last_tested = meta.get("last_tested")
        last_test_passed = meta.get("last_test_passed")
        cred_env_keys: list[str] = meta.get("credentials_env_keys", [])

        # Check which keys are currently set (value presence, not value)
        credentials_env_status = {key: bool(os.getenv(key)) for key in cred_env_keys}

        if status == "active":
            active_count += 1
        if last_test_passed is False:
            failing_count += 1

        agent_statuses.append(
            {
                "source_type": stype,
                "name": meta.get("name", stype),
                "status": status,
                "last_tested": last_tested,
                "last_test_passed": last_test_passed,
                "credentials_env_keys": cred_env_keys,
                "credentials_env_status": credentials_env_status,
            }
        )

    summary = (
        f"{total} agent(s) registered — "
        f"{active_count} active, "
        f"{total - active_count} inactive, "
        f"{failing_count} with a failing last test."
    )

    return {
        "agents": agent_statuses,
        "summary": summary,
    }
