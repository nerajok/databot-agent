"""
Agent Factory tools — ADK tool functions that power the factory agent.

Routing priority for any new source_type:
  1. MCP registry  — source has a known MCP server → build MCPToolset agent, no code gen
  2. Connector registry — source has a BaseConnector class → use existing connector
  3. Neither → return needs_connector_generation, root_agent delegates to resolution_agent
     After resolution_agent validates the connector, factory calls
     complete_registration_after_resolution() to finish.

Tools:
  1. list_registered_agents
  2. get_credential_prompt
  3. test_and_register_agent
  4. complete_registration_after_resolution
  5. query_registered_source
"""

import logging
import os
from pathlib import Path

from dotenv import set_key
from google.adk.tools.tool_context import ToolContext

try:
    from ..agent_registry.registry import AgentRegistry
    from ..connectors import CONNECTOR_REGISTRY, get_connector, list_supported_types, reload_registry
    from .agent_builder import (
        build_agent, build_mcp_agent,
        write_agent_file, write_mcp_agent_file,
        get_mcp_params, list_mcp_types,
    )
    from .agent_store import add_agent as _add_to_root, get_model, list_live_agent_names
    from .auth_manager import (
        get_preferred_auth_method, build_credential_fields, build_credential_prompt_text,
        perform_oauth2_exchange, perform_oauth2_authcode_flow, store_oauth_token, refresh_oauth_token_for,
        AUTH_METHOD_OAUTH2_AUTHCODE, AUTH_METHOD_OAUTH2, AUTH_METHOD_BASIC, AUTH_METHOD_CUSTOM,
    )
except ImportError:
    from databot_agent.agent_registry.registry import AgentRegistry
    from databot_agent.connectors import CONNECTOR_REGISTRY, get_connector, list_supported_types, reload_registry
    from databot_agent.agent_factory.agent_builder import (
        build_agent, build_mcp_agent,
        write_agent_file, write_mcp_agent_file,
        get_mcp_params, list_mcp_types,
    )
    from databot_agent.agent_factory.agent_store import (
        add_agent as _add_to_root, get_model, list_live_agent_names,
    )
    from databot_agent.agent_factory.auth_manager import (
        get_preferred_auth_method, build_credential_fields, build_credential_prompt_text,
        perform_oauth2_exchange, perform_oauth2_authcode_flow, store_oauth_token, refresh_oauth_token_for,
        AUTH_METHOD_OAUTH2_AUTHCODE, AUTH_METHOD_OAUTH2, AUTH_METHOD_BASIC, AUTH_METHOD_CUSTOM,
    )

_ENV_PATH = Path(__file__).parents[2] / ".env"

_BUILTIN_TYPES = {"tableau", "bigquery"}


# ---------------------------------------------------------------------------
# Tool 1: List registered agents
# ---------------------------------------------------------------------------

def list_registered_agents(tool_context: ToolContext) -> dict:
    """
    Return all currently registered data-source agents and their status.
    Includes built-in agents (tableau, bigquery) and any dynamically created agents.
    Also shows which MCP-backed types and connector types can be created on demand.
    """
    registry = AgentRegistry.get()
    agents = registry.list_agents()
    live_names = list_live_agent_names()

    summary = []
    for key, meta in agents.items():
        summary.append({
            "type": key,
            "name": meta.get("name", key),
            "status": meta.get("status", "unknown"),
            "agent_type": meta.get("agent_type", "builtin"),
            "live_agent_active": f"{key}_agent" in live_names,
            "capabilities": meta.get("capabilities", []),
            "description": meta.get("description", ""),
            "last_tested": meta.get("last_tested", "never"),
        })

    return {
        "status": "success",
        "registered_count": len(agents),
        "registered_agents": summary,
        "live_agents": live_names,
        "mcp_backed_types": list_mcp_types(),
        "connector_types": list_supported_types(),
        "note": (
            "MCP-backed types have full API access (no code generation). "
            "Connector types use a BaseConnector class. "
            "Unknown types → resolution_agent generates a connector. "
            "Call get_credential_prompt(source_type) to start registration."
        ),
    }


# ---------------------------------------------------------------------------
# Tool 2: Generate credential prompt (with MCP routing)
# ---------------------------------------------------------------------------

def get_credential_prompt(tool_context: ToolContext, source_type: str) -> dict:
    """
    Determine what credentials are needed for a new data source and return a
    human-readable prompt to show the user.

    Routing:
      1. Already registered → return current status
      2. MCP registry → show required env vars for MCP server
      3. Connector registry → show connector credential form
      4. Neither → return needs_connector_generation (signal for resolution_agent)

    Args:
        source_type: e.g. 'slack', 'salesforce', 'snowflake', 'jira'
    """
    source_type = source_type.lower().strip()

    # 0. Already registered?
    registry = AgentRegistry.get()
    if registry.is_registered(source_type):
        existing = registry.get_agent(source_type)
        return {
            "status": "already_registered",
            "message": (
                f"A '{source_type}' agent is already registered "
                f"(status: {existing.get('status')}, type: {existing.get('agent_type', 'unknown')})."
            ),
            "agent": existing,
        }

    # 1. MCP registry first
    mcp_params = get_mcp_params(source_type)
    if mcp_params:
        env_keys = mcp_params.get("env_keys", [])
        missing = [k for k in env_keys if not os.getenv(k)]

        tool_context.state["pending_connector_type"] = source_type
        tool_context.state["pending_is_mcp"] = True
        tool_context.state["pending_credential_fields"] = env_keys

        if missing:
            prompt = (
                f"To add {mcp_params['display_name']}, please provide the following:\n\n"
                + "\n".join(f"  - {k}" for k in missing)
                + "\n\nOnce provided, I'll test the MCP server connection and register it."
            )
        else:
            prompt = (
                f"All credentials for {mcp_params['display_name']} are already in your environment. "
                f"Call test_and_register_agent('{source_type}', {{}}) to register it now."
            )

        return {
            "status": "mcp_credentials_needed",
            "source_type": source_type,
            "display_name": mcp_params["display_name"],
            "description": mcp_params["description"],
            "capabilities": mcp_params.get("capabilities", []),
            "mcp_server": f"{mcp_params['command']} {' '.join(mcp_params['args'])}",
            "env_keys_needed": env_keys,
            "missing_env_keys": missing,
            "prompt_for_user": prompt,
            "instruction": (
                "Show prompt_for_user verbatim. If missing_env_keys is empty, call "
                "test_and_register_agent() with an empty credentials dict. "
                "Otherwise collect missing values from the user and call "
                "test_and_register_agent() with {KEY: value} for each missing key."
            ),
        }

    # 2. Connector registry
    connector_cls = get_connector(source_type)
    if connector_cls:
        auth_method = get_preferred_auth_method(connector_cls)
        fields = build_credential_fields(connector_cls, auth_method)
        prompt_text = build_credential_prompt_text(connector_cls, auth_method, fields)

        tool_context.state["pending_connector_type"] = source_type
        tool_context.state["pending_is_mcp"] = False
        tool_context.state["pending_auth_method"] = auth_method
        tool_context.state["pending_credential_fields"] = [f["key"] for f in fields]

        auth_label = {
            AUTH_METHOD_OAUTH2_AUTHCODE: "OAuth 2.0 (authorization_code) — browser login required",
            AUTH_METHOD_OAUTH2: "OAuth 2.0 (client_credentials) — token exchange done automatically",
            AUTH_METHOD_BASIC: "Username / Password",
            AUTH_METHOD_CUSTOM: "Custom API credentials",
        }.get(auth_method, auth_method)

        if auth_method == AUTH_METHOD_OAUTH2_AUTHCODE:
            instruction = (
                "OAuth 2.0 authorization code flow selected. "
                "Show prompt_for_user verbatim to the user. "
                "Collect the credential values (Client ID, Client Secret, Authorization URL, Token URL, and optionally Scope). "
                f"Then call test_and_register_agent(source_type='{source_type}', credentials={{...}}, auth_method='oauth2_authcode'). "
                "A browser window will open automatically for the user to log in and authorize access. "
                "The agent will capture the auth code from the redirect and exchange it for tokens — "
                "the user does NOT need to paste anything after authorizing. "
                "If the user prefers a different auth method, call get_credential_prompt() again with a different auth_method."
            )
        else:
            instruction = (
                f"Auth method selected: {auth_method}. "
                "Show prompt_for_user verbatim to the user. "
                "Collect the credential values they provide. "
                f"Then call test_and_register_agent(source_type='{source_type}', credentials={{...}}, auth_method='{auth_method}'). "
                "If the user prefers a different auth method, call get_credential_prompt() again "
                "with auth_method overridden in the credentials dict (advanced use)."
            )

        return {
            "status": "credentials_needed",
            "source_type": source_type,
            "display_name": connector_cls.DISPLAY_NAME,
            "description": connector_cls.DESCRIPTION,
            "auth_method": auth_method,
            "auth_label": auth_label,
            "auth_methods_supported": getattr(connector_cls, "AUTH_METHODS", []) or ["custom"],
            "prompt_for_user": prompt_text,
            "required_fields": fields,
            "instruction": instruction,
        }

    # 3. Neither — signal resolution_agent
    tool_context.state["pending_connector_type"] = source_type
    tool_context.state["pending_is_mcp"] = False

    return {
        "status": "needs_connector_generation",
        "source_type": source_type,
        "message": (
            f"No built-in connector or MCP server found for '{source_type}'. "
            "The resolution_agent will generate a connector automatically."
        ),
        "mcp_types": list_mcp_types(),
        "connector_types": list_supported_types(),
        "action": (
            f"Ask the user to describe the '{source_type}' API: "
            "base URL, authentication method, what data it provides, any documentation URL. "
            "Also ask for the credentials needed (API keys, tokens, etc.). "
            "Store the description in state as 'pending_source_description' and "
            "the credentials as 'pending_credentials'. "
            "Then tell root_agent to delegate to resolution_agent, passing "
            f"source_type='{source_type}' and the user's API description."
        ),
    }


# ---------------------------------------------------------------------------
# Tool 3: Test connection and register (routes to MCP / connector / unknown)
# ---------------------------------------------------------------------------

def test_and_register_agent(
    tool_context: ToolContext,
    source_type: str,
    credentials: dict,
    auth_method: str = "auto",
) -> dict:
    """
    Test a connection to a new data source and register it.

    Routing:
      1. MCP registry → verify env vars, build MCPToolset agent, write static MCP file
      2. Connector registry → OAuth 2.0 token exchange (if supported) → test → register
      3. Neither → store credentials in state, return needs_connector_generation

    Args:
        source_type:  e.g. 'slack', 'salesforce', 'snowflake', 'jira'
        credentials:  {ENV_VAR_KEY: value} for the credential fields from get_credential_prompt.
                      Pass {} if all credentials are already in the environment.
        auth_method:  "auto" (use preferred from connector) | "oauth2" | "basic" | "custom".
                      "auto" reads pending_auth_method from state set by get_credential_prompt.
    """
    source_type = source_type.lower().strip()

    # Route 1: MCP
    mcp_params = get_mcp_params(source_type)
    if mcp_params:
        return _register_mcp_agent(tool_context, source_type, mcp_params, credentials)

    # Route 2: Connector
    connector_cls = get_connector(source_type)
    if connector_cls:
        # Resolve auth_method: explicit arg > state > connector preference
        resolved_auth = auth_method
        if resolved_auth == "auto":
            resolved_auth = (
                tool_context.state.get("pending_auth_method")
                or get_preferred_auth_method(connector_cls)
            )
        return _register_connector_agent(
            tool_context, source_type, connector_cls, credentials, resolved_auth
        )

    # Route 3: Unknown — store credentials, signal resolution_agent
    if credentials:
        tool_context.state["pending_credentials"] = credentials
        _write_credentials_to_env(credentials)

    return {
        "status": "needs_connector_generation",
        "source_type": source_type,
        "message": (
            f"No connector found for '{source_type}'. "
            "Credentials stored in state. "
            "Root agent should now delegate to resolution_agent to generate the connector."
        ),
        "credentials_stored": list(credentials.keys()) if credentials else [],
    }


def _register_mcp_agent(
    tool_context: ToolContext,
    source_type: str,
    mcp_params: dict,
    credentials: dict,
) -> dict:
    """Register an MCP-backed agent: write env vars, build agent, write static file."""
    # 1. Write provided credentials to .env and os.environ
    if credentials:
        _write_credentials_to_env(credentials)

    # 2. Verify all required env vars are now set
    env_keys = mcp_params.get("env_keys", [])
    missing = [k for k in env_keys if not os.getenv(k)]
    if missing:
        return {
            "status": "missing_credentials",
            "source_type": source_type,
            "missing_env_keys": missing,
            "message": (
                f"Still missing required env vars: {missing}. "
                "Please provide the values and call test_and_register_agent() again."
            ),
        }

    # 3. Register in agent registry
    registry = AgentRegistry.get()
    registry.register(
        source_type,
        {
            "name": f"{mcp_params['display_name']} Agent",
            "status": "active",
            "capabilities": mcp_params.get("capabilities", []),
            "description": mcp_params["description"],
            "credentials_env_keys": env_keys,
            "agent_type": "mcp",
            "mcp_command": mcp_params["command"],
            "mcp_args": mcp_params["args"],
            "last_test_passed": True,
        },
    )
    registry.update_last_tested(source_type, success=True)

    # 4. Build live MCP agent and add to root
    agent_added = False
    agent_name = f"{source_type}_agent"
    try:
        model = get_model()
        if model is None:
            raise RuntimeError("agent_store model not set — was agent_store.init() called?")
        new_agent = build_mcp_agent(source_type, mcp_params, model)
        agent_added = _add_to_root(new_agent)
        logging.info(f"AgentFactory: MCP agent '{agent_name}' added = {agent_added}")
    except Exception as exc:
        logging.error(f"AgentFactory: failed to build MCP agent for '{source_type}': {exc}")
        agent_name = None

    # 5. Write static MCP agent file
    agent_file_path = None
    try:
        agent_file_path = str(write_mcp_agent_file(source_type, mcp_params))
    except Exception as exc:
        logging.error(f"AgentFactory: failed to write MCP agent file for '{source_type}': {exc}")

    # Clear pending state
    _clear_pending_state(tool_context, source_type)

    return {
        "status": "registered",
        "source_type": source_type,
        "display_name": mcp_params["display_name"],
        "agent_type": "mcp",
        "capabilities": mcp_params.get("capabilities", []),
        "credentials_saved_to_env": list(credentials.keys()) if credentials else [],
        "live_agent_created": agent_added,
        "live_agent_name": agent_name,
        "static_file_written": agent_file_path,
        "next_step": (
            f"'{agent_name}' is live — backed by an MCP server with full API access. "
            f"Static file written to source_agents/{source_type}_agent.py. "
            "The root agent will delegate to it automatically for future queries."
        ),
    }


def _register_connector_agent(
    tool_context: ToolContext,
    source_type: str,
    connector_cls,
    credentials: dict,
    auth_method: str = AUTH_METHOD_CUSTOM,
) -> dict:
    """
    Register a connector-backed agent.

    Auth flow:
      OAuth 2.0 → exchange client_credentials → store access token → test → register
      Basic / Custom → test directly → register
    """
    # 1. OAuth 2.0: perform token acquisition before anything else
    oauth_token_key: str | None = None
    if auth_method in (AUTH_METHOD_OAUTH2_AUTHCODE, AUTH_METHOD_OAUTH2):
        if auth_method == AUTH_METHOD_OAUTH2_AUTHCODE:
            logging.info("AgentFactory: starting OAuth2 auth code flow for '%s'", source_type)
            token_result = perform_oauth2_authcode_flow(credentials, source_type)
            grant_label = "authorization_code"
        else:
            logging.info("AgentFactory: performing OAuth2 client_credentials exchange for '%s'", source_type)
            token_result = perform_oauth2_exchange(credentials, source_type)
            grant_label = "client_credentials"

        if token_result["status"] != "success":
            return {
                "status": "oauth2_failed",
                "source_type": source_type,
                "auth_method": auth_method,
                "error": token_result["error"],
                "action_needed": (
                    f"OAuth 2.0 {grant_label} flow failed. "
                    "Check CLIENT_ID, CLIENT_SECRET, and the URL fields. "
                    "To fall back to username/password, call test_and_register_agent() "
                    f"again with auth_method='basic'."
                ),
            }

        # Store token(s) in .env and inject access token into credentials for the connection test
        oauth_token_key = store_oauth_token(source_type, token_result, _ENV_PATH)
        credentials = dict(credentials)  # don't mutate caller's dict
        credentials[oauth_token_key] = token_result["access_token"]
        logging.info(
            "AgentFactory: OAuth2 %s succeeded for '%s' (expires_in=%s)",
            grant_label, source_type, token_result.get("expires_in"),
        )

    # 2. Write non-secret credential fields to .env (token already written above if OAuth2)
    _write_credentials_to_env(
        {k: v for k, v in credentials.items() if k != oauth_token_key}
        if oauth_token_key
        else credentials
    )

    # 3. Test connection (uses the token from env if OAuth2)
    logging.info("AgentFactory: testing %s connection (auth=%s)...", source_type, auth_method)
    test_result = connector_cls.test_connection(credentials)

    if test_result["status"] == "missing_package":
        return {
            "status": "missing_package",
            "source_type": source_type,
            "message": test_result["message"],
            "install_command": test_result.get("install_command", ""),
            "action_needed": (
                f"Ask the user to run: {test_result.get('install_command', 'pip install <package>')} "
                "then retry."
            ),
        }

    if test_result["status"] != "success":
        # OAuth2 succeeded but connection still failed → suggest basic auth fallback
        fallback_hint = (
            " You may also try auth_method='basic' if the API supports it."
            if auth_method == AUTH_METHOD_OAUTH2
            else ""
        )
        return {
            "status": "connection_failed",
            "source_type": source_type,
            "auth_method": auth_method,
            "error": test_result["message"],
            "action_needed": f"Check the credentials and try again.{fallback_hint}",
        }

    # 4. Register in agent registry
    registry = AgentRegistry.get()
    registry.register(
        source_type,
        {
            "name": f"{connector_cls.DISPLAY_NAME} Agent",
            "status": "active",
            "capabilities": connector_cls.CAPABILITIES,
            "description": connector_cls.DESCRIPTION,
            "credentials_env_keys": list(credentials.keys()),
            "agent_type": "connector",
            "auth_method": auth_method,
            "last_test_passed": True,
        },
    )
    registry.update_last_tested(source_type, success=True)

    # 5. Build live agent and add to root
    agent_added = False
    agent_name = f"{source_type}_agent"
    try:
        model = get_model()
        if model is None:
            raise RuntimeError("agent_store model not set — was agent_store.init() called?")
        new_agent = build_agent(source_type, model)
        agent_added = _add_to_root(new_agent)
        logging.info("AgentFactory: connector agent '%s' added = %s", agent_name, agent_added)
    except Exception as exc:
        logging.error("AgentFactory: failed to build agent for '%s': %s", source_type, exc)
        agent_name = None

    # 6. Write static agent file
    agent_file_path = None
    try:
        agent_file_path = str(write_agent_file(source_type))
    except Exception as exc:
        logging.error("AgentFactory: failed to write static agent file for '%s': %s", source_type, exc)

    _clear_pending_state(tool_context, source_type)

    if auth_method == AUTH_METHOD_OAUTH2_AUTHCODE:
        auth_summary = f"OAuth 2.0 authorization_code (token stored as {oauth_token_key})"
    elif auth_method == AUTH_METHOD_OAUTH2:
        auth_summary = f"OAuth 2.0 client_credentials (token stored as {oauth_token_key})"
    else:
        auth_summary = auth_method

    return {
        "status": "registered",
        "source_type": source_type,
        "display_name": connector_cls.DISPLAY_NAME,
        "agent_type": "connector",
        "auth_method": auth_method,
        "auth_summary": auth_summary,
        "connection_message": test_result["message"],
        "capabilities": connector_cls.CAPABILITIES,
        "credentials_saved_to_env": list(credentials.keys()),
        "live_agent_created": agent_added,
        "live_agent_name": agent_name,
        "static_file_written": agent_file_path,
        "next_step": (
            f"'{agent_name}' is now live ({auth_summary}). "
            f"Static file written to source_agents/{source_type}_agent.py. "
            "The root agent will delegate to it automatically for future queries. "
            + (
                f"OAuth2 tokens expire — call refresh_oauth_token('{source_type}') "
                "to get a fresh token when queries start returning auth errors."
                if auth_method in (AUTH_METHOD_OAUTH2_AUTHCODE, AUTH_METHOD_OAUTH2)
                else ""
            )
        ),
    }


# ---------------------------------------------------------------------------
# Tool 4: Complete registration after resolution_agent validates a connector
# ---------------------------------------------------------------------------

def complete_registration_after_resolution(
    tool_context: ToolContext,
    source_type: str,
) -> dict:
    """
    Called by agent_factory_agent after resolution_agent has successfully
    generated and validated a new connector in connectors/<source_type>.py.

    This tool:
      1. Reloads the connector registry to pick up the new file
      2. Confirms the connector loaded correctly
      3. Tests the connection using pending_credentials from state
      4. Writes credentials to .env
      5. Registers in agent registry
      6. Builds a live ADK Agent and adds it to root_agent.sub_agents
      7. Writes the static agent file to source_agents/
      8. Clears pending state

    Args:
        source_type: the source type that resolution_agent just validated
    """
    source_type = source_type.lower().strip()

    # 1. Reload connector registry
    try:
        reload_registry()
    except Exception as exc:
        return {
            "status": "error",
            "source_type": source_type,
            "message": f"Failed to reload connector registry: {exc}",
        }

    # 2. Confirm the connector loaded
    connector_cls = get_connector(source_type)
    if not connector_cls:
        return {
            "status": "connector_not_found",
            "source_type": source_type,
            "message": (
                f"Connector for '{source_type}' was not found after reload. "
                "resolution_agent may have written the file with a bug. "
                "Delegate back to resolution_agent to fix it."
            ),
        }

    # 3. Get credentials from state
    credentials = tool_context.state.get("pending_credentials", {})

    # 4. Test the connection
    test_result = connector_cls.test_connection(credentials)
    if test_result["status"] != "success":
        return {
            "status": "connection_failed",
            "source_type": source_type,
            "error": test_result["message"],
            "action_needed": (
                "Connection test failed after resolution. "
                "Delegate to resolution_agent with the error message to fix the connector."
            ),
        }

    # 5. Write credentials to .env
    if credentials:
        _write_credentials_to_env(credentials)

    # 6. Register in agent registry
    registry = AgentRegistry.get()
    registry.register(
        source_type,
        {
            "name": f"{connector_cls.DISPLAY_NAME} Agent",
            "status": "active",
            "capabilities": getattr(connector_cls, "CAPABILITIES", []),
            "description": getattr(connector_cls, "DESCRIPTION", ""),
            "credentials_env_keys": list(credentials.keys()),
            "agent_type": "connector_generated",
            "last_test_passed": True,
        },
    )
    registry.update_last_tested(source_type, success=True)

    # 7. Build live agent and add to root
    agent_added = False
    agent_name = f"{source_type}_agent"
    try:
        model = get_model()
        if model is None:
            raise RuntimeError("agent_store model not set")
        new_agent = build_agent(source_type, model)
        agent_added = _add_to_root(new_agent)
        logging.info(f"AgentFactory: post-resolution agent '{agent_name}' added = {agent_added}")
    except Exception as exc:
        logging.error(f"AgentFactory: failed to build post-resolution agent '{source_type}': {exc}")
        agent_name = None

    # 8. Write static agent file
    agent_file_path = None
    try:
        agent_file_path = str(write_agent_file(source_type))
    except Exception as exc:
        logging.error(f"AgentFactory: failed to write static file for '{source_type}': {exc}")

    # 9. Clear all pending state
    _clear_pending_state(tool_context, source_type)
    tool_context.state.pop("pending_credentials", None)
    tool_context.state.pop("pending_source_description", None)

    return {
        "status": "registered",
        "source_type": source_type,
        "display_name": connector_cls.DISPLAY_NAME,
        "agent_type": "connector_generated",
        "capabilities": getattr(connector_cls, "CAPABILITIES", []),
        "credentials_saved_to_env": list(credentials.keys()),
        "live_agent_created": agent_added,
        "live_agent_name": agent_name,
        "static_file_written": agent_file_path,
        "next_step": (
            f"'{agent_name}' is now live. resolution_agent wrote the connector, "
            "the factory tested it, and a static file was generated for future sessions. "
            "The root agent will delegate to it for all future queries."
        ),
    }


# ---------------------------------------------------------------------------
# Tool 5: Refresh an OAuth 2.0 access token
# ---------------------------------------------------------------------------

def refresh_oauth_token(tool_context: ToolContext, source_type: str) -> dict:
    """
    Re-exchange stored OAuth 2.0 credentials for a fresh access token.

    Reads CLIENT_ID, CLIENT_SECRET, TOKEN_URL from the environment
    (written there during initial registration) and performs a new
    client_credentials grant. The new token overwrites the old one in .env.

    Call this when a registered OAuth2-backed agent starts returning
    authentication errors (expired token).

    Args:
        source_type: the registered source type, e.g. 'salesforce', 'snowflake'
    """
    source_type = source_type.lower().strip()

    registry = AgentRegistry.get()
    agent_meta = registry.get_agent(source_type)

    if not agent_meta:
        return {
            "status": "not_registered",
            "source_type": source_type,
            "message": f"'{source_type}' is not registered. Register it first via agent_factory_agent.",
        }

    stored_auth_method = agent_meta.get("auth_method", AUTH_METHOD_CUSTOM)
    if stored_auth_method != AUTH_METHOD_OAUTH2:
        return {
            "status": "not_oauth2",
            "source_type": source_type,
            "auth_method": stored_auth_method,
            "message": (
                f"'{source_type}' was registered with auth_method='{stored_auth_method}', "
                "not OAuth 2.0. Token refresh is only applicable to OAuth2-registered sources."
            ),
        }

    result = refresh_oauth_token_for(source_type, _ENV_PATH)

    if result["status"] != "success":
        return {
            "status": "refresh_failed",
            "source_type": source_type,
            "error": result["error"],
            "action_needed": (
                "Check that CLIENT_ID, CLIENT_SECRET, and TOKEN_URL are still valid in .env. "
                "If credentials changed, re-register via test_and_register_agent()."
            ),
        }

    registry.update_last_tested(source_type, success=True)

    return {
        "status": "refreshed",
        "source_type": source_type,
        "token_key": result.get("token_key"),
        "expires_in": result.get("expires_in"),
        "message": (
            f"OAuth2 access token refreshed for '{source_type}'. "
            f"New token stored as {result.get('token_key')}. "
            "The agent will use it automatically on the next query."
        ),
    }


# ---------------------------------------------------------------------------
# Tool 6: Query any registered source
# ---------------------------------------------------------------------------

def query_registered_source(
    tool_context: ToolContext,
    source_type: str,
    query: str,
    query_type: str = "auto",
) -> dict:
    """
    Route a query to any registered connector-backed data source.

    Works for connector-backed sources: salesforce (SOQL), snowflake (SQL),
    rest_api (path), and custom generated connectors.
    MCP-backed sources (slack, github, etc.) should be queried via their
    dedicated <source>_agent directly.

    Args:
        source_type:  Registered source type key (e.g. 'salesforce').
        query:        Query string — SOQL for Salesforce, SQL for Snowflake,
                      URL path for REST APIs.
        query_type:   Hint: 'soql' | 'sql' | 'http_get' | 'auto'.
    """
    registry = AgentRegistry.get()
    agent_meta = registry.get_agent(source_type)

    if not agent_meta:
        return {
            "status": "not_registered",
            "source_type": source_type,
            "message": (
                f"'{source_type}' is not in the registry. "
                "Call get_credential_prompt() then test_and_register_agent() first."
            ),
            "registered_types": list(registry.list_agents().keys()),
        }

    if agent_meta.get("status") != "active":
        return {
            "status": "agent_inactive",
            "source_type": source_type,
            "message": f"Agent '{source_type}' status is '{agent_meta.get('status')}', not active.",
        }

    if agent_meta.get("agent_type") == "mcp":
        return {
            "status": "use_dedicated_agent",
            "source_type": source_type,
            "message": (
                f"'{source_type}' is MCP-backed. Delegate to '{source_type}_agent' directly "
                "rather than using query_registered_source."
            ),
        }

    if source_type in _BUILTIN_TYPES:
        return {
            "status": "use_dedicated_agent",
            "source_type": source_type,
            "message": (
                f"'{source_type}' is a built-in agent. "
                "Use tableau_inspector_agent or bigquery_analyst_agent directly."
            ),
        }

    connector_cls = get_connector(source_type)
    if not connector_cls:
        return {
            "status": "connector_not_found",
            "source_type": source_type,
            "message": (
                f"No connector class found for '{source_type}'. "
                "The connector may have been removed or failed to load."
            ),
        }

    logging.info(f"AgentFactory: querying '{source_type}' — {query[:80]}")
    result = connector_cls.query(query, query_type=query_type)
    registry.update_last_tested(source_type, success=(result["status"] == "success"))
    return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_credentials_to_env(credentials: dict) -> None:
    """Write credential key-value pairs to the .env file and os.environ."""
    if not _ENV_PATH.exists():
        _ENV_PATH.touch()
    for key, value in credentials.items():
        if value:
            set_key(str(_ENV_PATH), key, str(value))
            os.environ[key] = str(value)
            logging.info(f"AgentFactory: wrote {key} to .env")


def _clear_pending_state(tool_context: ToolContext, source_type: str) -> None:
    """Clear pending factory state and mark the source as available."""
    tool_context.state.pop("pending_connector_type", None)
    tool_context.state.pop("pending_is_mcp", None)
    tool_context.state.pop("pending_credential_fields", None)
    tool_context.state[f"{source_type}_available"] = True
