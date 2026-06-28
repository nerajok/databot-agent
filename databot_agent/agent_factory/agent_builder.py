"""
Builds full ADK Agent objects for dynamically registered data sources.

Two entry points:
  build_agent(source_type, model)  — build an in-memory ADK Agent for this session
  write_agent_file(source_type)    — generate a static source_agents/<type>_agent.py
                                     that is imported on future startups (no rebuild needed)

The static file is human-editable: developers can add more tools, enrich the
instruction, or add source-specific error handling after the factory creates it.
"""

import logging
from pathlib import Path

from google.adk import Agent
from google.adk.tools.tool_context import ToolContext

try:
    from ..connectors import get_connector
except ImportError:
    from databot_agent.connectors import get_connector

_SOURCE_AGENTS_DIR = Path(__file__).parents[1] / "source_agents"

# ---------------------------------------------------------------------------
# MCP server registry
#
# Maps source_type → MCP server connection parameters for sources that have
# a published MCP server package. The factory checks this FIRST before
# falling back to connector classes or the resolution agent.
#
# Each entry: {command, args, env_keys, display_name, description, capabilities}
# env_keys: list of env var names the server needs (populated from .env)
# ---------------------------------------------------------------------------

_MCP_REGISTRY: dict[str, dict] = {
    "slack": {
        "display_name": "Slack",
        "description": "Read Slack messages, channels, and user data.",
        "capabilities": ["search_messages", "list_channels", "get_user"],
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-slack"],
        "env_keys": ["SLACK_BOT_TOKEN"],
    },
    "github": {
        "display_name": "GitHub",
        "description": "Search repos, issues, PRs, and code on GitHub.",
        "capabilities": ["search_repos", "list_issues", "get_pr", "search_code"],
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-github"],
        "env_keys": ["GITHUB_PERSONAL_ACCESS_TOKEN"],
    },
    "notion": {
        "display_name": "Notion",
        "description": "Read and search Notion pages and databases.",
        "capabilities": ["search", "get_page", "query_database"],
        "command": "npx",
        "args": ["-y", "@notionhq/notion-mcp-server"],
        "env_keys": ["NOTION_API_KEY"],
    },
    "linear": {
        "display_name": "Linear",
        "description": "Read Linear issues, projects, and team data.",
        "capabilities": ["list_issues", "get_issue", "list_projects"],
        "command": "npx",
        "args": ["-y", "@linear/mcp-server"],
        "env_keys": ["LINEAR_API_KEY"],
    },
    "postgres": {
        "display_name": "PostgreSQL",
        "description": "Query PostgreSQL databases (read-only).",
        "capabilities": ["execute_sql", "list_tables", "describe_table"],
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-postgres"],
        "env_keys": ["POSTGRES_CONNECTION_STRING"],
    },
}


def get_mcp_params(source_type: str) -> dict | None:
    """Return MCP server params for source_type, or None if not in MCP registry."""
    return _MCP_REGISTRY.get(source_type.lower())


def list_mcp_types() -> list[str]:
    """Return source types backed by MCP servers."""
    return list(_MCP_REGISTRY.keys())


# ---------------------------------------------------------------------------
# Per-source-type agent instructions
# ---------------------------------------------------------------------------

_SALESFORCE_INSTRUCTION = """
You are the Salesforce CRM data specialist. Your job is to retrieve CRM data
that may explain discrepancies in Tableau dashboards — such as opportunity
counts, pipeline value, customer status, or product usage metrics.

**Query language:** SOQL (Salesforce Object Query Language)
**Tool:** `query_salesforce`

**WORKFLOW:**

1. Based on the dashboard issue in agent state, identify what CRM object to query
   (e.g. Opportunity, Account, Contract, Lead, CustomObject__c).

2. Write SOQL queries to retrieve:
   - Aggregate values matching the dashboard metric's time range and filters
   - Status/stage distributions that could inflate or deflate the metric
   - Recent changes (CreatedDate, LastModifiedDate) that might indicate data lag

3. Example queries:
   - `SELECT SUM(Amount) total FROM Opportunity WHERE CloseDate = THIS_QUARTER AND StageName = 'Closed Won'`
   - `SELECT COUNT(Id) cnt, Status FROM Contract WHERE StartDate = THIS_YEAR GROUP BY Status`

4. Call `query_salesforce` for each query. Summarise:
   - What the Salesforce data shows for the metric
   - How it compares to the Tableau dashboard value
   - Any anomalies (e.g. duplicate records, missing stage filters, timezone issues)

**Important:** Only run SELECT queries. Never use DML (INSERT, UPDATE, DELETE, UPSERT).
"""

_SNOWFLAKE_INSTRUCTION = """
You are the Snowflake data warehouse specialist. Your job is to query Snowflake
tables to retrieve the ground-truth data underlying a Tableau dashboard.

**Query language:** Standard SQL (Snowflake dialect)
**Tool:** `query_snowflake`

**STRICT READ-ONLY POLICY:**
- Only SELECT or WITH (CTE) queries are permitted.
- Never use INSERT, UPDATE, DELETE, DROP, CREATE, ALTER, or MERGE.

**WORKFLOW:**

1. From agent state, identify the schema and table that the Tableau dashboard reads.

2. Write SELECT queries to retrieve:
   - The aggregate metric for the relevant time period
   - Row counts and data freshness (MAX(updated_at) or similar)
   - Distributions of key categorical columns (status, type, region)

3. Call `query_snowflake` for each query and record the results.

4. Summarise:
   - What Snowflake shows for the metric vs what Tableau displays
   - Whether the discrepancy is from a filter difference, stale data, or schema issue
"""

_REST_API_INSTRUCTION = """
You are the REST API data specialist. Your job is to fetch data from an external
REST API endpoint to retrieve metrics that a Tableau dashboard may be pulling from.

**Tool:** `query_rest_api`
**Query format:** URL path (e.g. `/reports/monthly` or `/metrics?period=q2-2027`)

**WORKFLOW:**

1. From the dashboard issue context, determine which API endpoint holds the
   relevant metric data.

2. Call `query_rest_api` with the appropriate path.
   - Use `method="GET"` for most data fetching.
   - Inspect the JSON response structure to locate the metric value.

3. Summarise:
   - What the API returned for the metric
   - How it compares to the Tableau dashboard value
   - Any pagination, rate-limit, or staleness indicators in the response
"""

_GENERIC_INSTRUCTION_TEMPLATE = """
You are the {display_name} data specialist. Your job is to query {display_name}
to retrieve data relevant to the Tableau dashboard diagnostic investigation.

**Tool:** `query_{source_type}`
**Capabilities:** {capabilities}

**WORKFLOW:**

1. Based on the dashboard issue in agent state, determine what query to run.

2. Call `query_{source_type}` with an appropriate query string.

3. Summarise:
   - What the data shows for the metric in question
   - How it compares to the Tableau dashboard value
   - Any anomalies or data quality issues
"""

_INSTRUCTIONS = {
    "salesforce": _SALESFORCE_INSTRUCTION,
    "snowflake": _SNOWFLAKE_INSTRUCTION,
    "rest_api": _REST_API_INSTRUCTION,
}


# ---------------------------------------------------------------------------
# Tool builder
# ---------------------------------------------------------------------------

def _make_query_tool(connector_cls, source_type: str):
    """
    Return a ToolContext-compatible function that wraps connector_cls.query().
    Uses a closure so each source type gets a distinct function object with the
    right __name__ (ADK uses __name__ as the tool name in the model's tool list).
    """

    def query_tool(tool_context: ToolContext, query: str, query_type: str = "auto") -> dict:
        return connector_cls.query(query, query_type=query_type)

    query_tool.__name__ = f"query_{source_type}"
    query_tool.__doc__ = (
        f"Execute a query against {connector_cls.DISPLAY_NAME}. "
        f"{connector_cls.DESCRIPTION}"
    )
    return query_tool


# ---------------------------------------------------------------------------
# Agent builder
# ---------------------------------------------------------------------------

def build_agent(source_type: str, model) -> Agent:
    """
    Build a full ADK Agent for a registered data-source type.
    The returned agent mirrors the structure of tableau_inspector_agent and
    bigquery_analyst_agent: dedicated tool, typed instruction, and output_key.

    Args:
        source_type: registered connector key (e.g. 'salesforce', 'snowflake')
        model:       the model object (AnthropicLlm or str) from agent.py

    Returns:
        An ADK Agent instance ready to be appended to root_agent.sub_agents.

    Raises:
        ValueError: if source_type has no registered connector.
    """
    connector_cls = get_connector(source_type)
    if not connector_cls:
        raise ValueError(f"No connector registered for source type '{source_type}'")

    query_tool = _make_query_tool(connector_cls, source_type)

    instruction = _INSTRUCTIONS.get(source_type) or _GENERIC_INSTRUCTION_TEMPLATE.format(
        display_name=connector_cls.DISPLAY_NAME,
        source_type=source_type,
        capabilities=", ".join(getattr(connector_cls, "CAPABILITIES", [])),
    )

    agent = Agent(
        name=f"{source_type}_agent",
        model=model,
        description=(
            f"Queries {connector_cls.DISPLAY_NAME} to retrieve data for "
            "Tableau dashboard diagnostic analysis. "
            f"{connector_cls.DESCRIPTION}"
        ),
        instruction=instruction,
        tools=[query_tool],
        output_key=f"{source_type}_query_results",
    )

    logging.info(f"AgentBuilder: created '{agent.name}' with tool 'query_{source_type}'")
    return agent


# ---------------------------------------------------------------------------
# Static file writer
# ---------------------------------------------------------------------------

def write_agent_file(source_type: str) -> Path:
    """
    Generate a static agent .py file at source_agents/<source_type>_agent.py.

    The file follows the exact same pattern as tableau_inspector_agent — it has
    its own model setup, a named query tool function, and an Agent definition.
    It is imported directly on future startups (no rebuild, no credential prompt).

    Developers can enrich the file: add tools, refine the instruction, etc.

    Returns the path to the written file.
    """
    from datetime import datetime

    connector_cls = get_connector(source_type)
    if not connector_cls:
        raise ValueError(f"No connector registered for source type '{source_type}'")

    instruction = _INSTRUCTIONS.get(source_type) or _GENERIC_INSTRUCTION_TEMPLATE.format(
        display_name=connector_cls.DISPLAY_NAME,
        source_type=source_type,
        capabilities=", ".join(getattr(connector_cls, "CAPABILITIES", [])),
    )
    description = (
        f"Queries {connector_cls.DISPLAY_NAME} to retrieve data for "
        f"Databot diagnostic analysis. {connector_cls.DESCRIPTION}"
    )

    # Escape triple-quotes so the instruction embeds safely in a triple-quoted string.
    instruction_safe = instruction.replace('"""', r'\"\"\"')
    description_repr = repr(description)
    generated_on = datetime.now().strftime("%Y-%m-%d %H:%M")

    content = f'''\
"""
{connector_cls.DISPLAY_NAME} Agent — auto-generated by Databot Agent Factory.
Generated: {generated_on}

Customize this file freely:
  - Add more tool functions above `{source_type}_agent` and include them in tools=[...]
  - Refine the instruction for better query quality
  - Add source-specific error handling or retry logic
"""
import os

from dotenv import load_dotenv
from google.adk import Agent
from google.adk.tools.tool_context import ToolContext

load_dotenv()

_MODEL_NAME = os.getenv("MODEL", "gemini-2.0-flash")
if os.getenv("ANTHROPIC_API_KEY") and _MODEL_NAME.startswith("claude"):
    from google.adk.models.anthropic_llm import AnthropicLlm
    _MODEL = AnthropicLlm(model=_MODEL_NAME)
else:
    _MODEL = _MODEL_NAME


def query_{source_type}(tool_context: ToolContext, query: str, query_type: str = "auto") -> dict:
    """Execute a query against {connector_cls.DISPLAY_NAME}."""
    from databot_agent.connectors import get_connector as _get_connector
    return _get_connector("{source_type}").query(query, query_type=query_type)


{source_type}_agent = Agent(
    name="{source_type}_agent",
    model=_MODEL,
    description={description_repr},
    instruction="""{instruction_safe}""",
    tools=[query_{source_type}],
    output_key="{source_type}_query_results",
)
'''

    _SOURCE_AGENTS_DIR.mkdir(exist_ok=True)
    init_file = _SOURCE_AGENTS_DIR / "__init__.py"
    if not init_file.exists():
        init_file.write_text("")

    file_path = _SOURCE_AGENTS_DIR / f"{source_type}_agent.py"
    file_path.write_text(content)
    logging.info(f"AgentBuilder: wrote static agent file → {file_path}")
    return file_path


def build_mcp_agent(source_type: str, mcp_params: dict, model) -> Agent:
    """
    Build an ADK Agent whose tools come from an MCP server rather than a
    connector class. The MCPToolset launches the MCP server as a subprocess
    and exposes all its tools to the agent automatically.
    """
    import os
    from google.adk.tools.mcp_tool.mcp_toolset import MCPToolset, StdioServerParameters

    env = {k: os.getenv(k, "") for k in mcp_params.get("env_keys", [])}

    toolset = MCPToolset(
        connection_params=StdioServerParameters(
            command=mcp_params["command"],
            args=mcp_params["args"],
            env=env,
        )
    )

    instruction = f"""
You are the {mcp_params['display_name']} specialist. Use the MCP tools available
to you to retrieve data relevant to the diagnostic investigation.

Capabilities: {', '.join(mcp_params.get('capabilities', []))}

After retrieving data:
1. Summarise the key findings relevant to the question
2. Note any discrepancies compared to other data sources in agent state
3. Return structured results for the gap_analyzer_agent
"""

    agent = Agent(
        name=f"{source_type}_agent",
        model=model,
        description=f"Queries {mcp_params['display_name']} via MCP. {mcp_params['description']}",
        instruction=instruction,
        tools=[toolset],
        output_key=f"{source_type}_query_results",
    )
    logging.info(f"AgentBuilder: created MCP-backed '{agent.name}'")
    return agent


def write_mcp_agent_file(source_type: str, mcp_params: dict) -> Path:
    """
    Generate a static source_agents/<source_type>_agent.py that uses MCPToolset.
    On next startup this file is imported directly — no factory step needed.
    """
    from datetime import datetime

    env_keys = mcp_params.get("env_keys", [])
    env_setup = "\n".join(
        [f'    "{k}": os.getenv("{k}", ""),' for k in env_keys]
    )
    generated_on = datetime.now().strftime("%Y-%m-%d %H:%M")
    description_repr = repr(
        f"Queries {mcp_params['display_name']} via MCP. {mcp_params['description']}"
    )
    capabilities = ", ".join(mcp_params.get("capabilities", []))

    content = f'''\
"""
{mcp_params['display_name']} Agent — auto-generated by Databot Agent Factory (MCP-backed).
Generated: {generated_on}

This agent uses an MCP server for full API access. To add capabilities,
update the MCP server package or switch to a connector-based agent.
MCP server: {mcp_params['command']} {" ".join(mcp_params['args'])}
"""
import os

from dotenv import load_dotenv
from google.adk import Agent
from google.adk.tools.mcp_tool.mcp_toolset import MCPToolset, StdioServerParameters

load_dotenv()

_MODEL_NAME = os.getenv("MODEL", "gemini-2.0-flash")
if os.getenv("ANTHROPIC_API_KEY") and _MODEL_NAME.startswith("claude"):
    from google.adk.models.anthropic_llm import AnthropicLlm
    _MODEL = AnthropicLlm(model=_MODEL_NAME)
else:
    _MODEL = _MODEL_NAME

_toolset = MCPToolset(
    connection_params=StdioServerParameters(
        command="{mcp_params['command']}",
        args={mcp_params['args']},
        env={{
{env_setup}
        }},
    )
)

{source_type}_agent = Agent(
    name="{source_type}_agent",
    model=_MODEL,
    description={description_repr},
    instruction="""
You are the {mcp_params['display_name']} specialist. Use the MCP tools available
to retrieve data for Databot diagnostic analysis.

Capabilities: {capabilities}

After retrieving data:
1. Summarise key findings relevant to the question
2. Note discrepancies vs other data sources in agent state
3. Return structured results for the gap_analyzer_agent
""",
    tools=[_toolset],
    output_key="{source_type}_query_results",
)
'''

    _SOURCE_AGENTS_DIR.mkdir(exist_ok=True)
    file_path = _SOURCE_AGENTS_DIR / f"{source_type}_agent.py"
    file_path.write_text(content)
    logging.info(f"AgentBuilder: wrote MCP agent file → {file_path}")
    return file_path
