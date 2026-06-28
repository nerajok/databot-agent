"""
Databot Agent

Dynamic multi-source data diagnostic agent. The root agent reads each prompt
and decides which specialist agents to invoke — skipping agents that aren't
needed for the question at hand.

Available agents (root_agent selects dynamically):
  - tableau_inspector_agent  — Tableau REST API: verify dashboard issues, fetch view data
  - bigquery_analyst_agent   — BigQuery SQL: query underlying tables (read-only)
  - gap_analyzer_agent       — Reasoning: synthesise findings into root-cause report
  - agent_factory_agent      — Create new source agents on demand (Salesforce, Snowflake, etc.)
  - <source>_agent           — Dynamically created per registered external source

To add a new connector type, implement BaseConnector in connectors/ and
add it to CONNECTOR_REGISTRY in connectors/__init__.py.
"""

import logging
import os

from dotenv import load_dotenv
from google.adk import Agent
from google.adk.models.anthropic_llm import AnthropicLlm
from google.adk.tools.tool_context import ToolContext

# --- Integration imports ---

try:
    from .tableau_tools.tableau_api import (
        inspect_dashboard,
        get_view_data_sample,
    )
    TABLEAU_TOOLS_AVAILABLE = True
    logging.info("Tableau tools: loaded successfully")
except ImportError as e:
    logging.warning(f"Tableau tools not available: {e}")
    TABLEAU_TOOLS_AVAILABLE = False

try:
    from .bigquery_utils.bigquery_tools import (
        get_bigquery_toolset,
        get_datasource_info,
        prepare_diagnostic_queries,
        save_query_results,
    )
    bigquery_toolset = get_bigquery_toolset()
    BIGQUERY_AVAILABLE = bigquery_toolset is not None
    logging.info(f"BigQuery toolset: {'available' if BIGQUERY_AVAILABLE else 'unavailable (dummy mode)'}")
except ImportError as e:
    logging.warning(f"BigQuery tools not available: {e}")
    bigquery_toolset = None
    BIGQUERY_AVAILABLE = False
    get_datasource_info = None
    prepare_diagnostic_queries = None
    save_query_results = None

try:
    from .agent_factory.factory_tools import (
        list_registered_agents,
        get_credential_prompt,
        test_and_register_agent,
        complete_registration_after_resolution,
        refresh_oauth_token,
        query_registered_source,
    )
    from .agent_factory.agent_builder import (
        build_agent as _build_agent,
        build_mcp_agent as _build_mcp_agent,
        get_mcp_params as _get_mcp_params,
    )
    from .agent_factory.agent_store import init as _init_agent_store
    from .agent_registry.registry import AgentRegistry as _AgentRegistry
    from .connectors import CONNECTOR_REGISTRY as _CONNECTOR_REGISTRY
    FACTORY_TOOLS_AVAILABLE = True
    logging.info("Agent factory tools: loaded successfully")
except ImportError as e:
    logging.warning(f"Agent factory tools not available: {e}")
    FACTORY_TOOLS_AVAILABLE = False
    list_registered_agents = None
    get_credential_prompt = None
    test_and_register_agent = None
    complete_registration_after_resolution = None
    refresh_oauth_token = None
    query_registered_source = None
    _build_agent = None
    _build_mcp_agent = None
    _get_mcp_params = None
    _init_agent_store = None
    _AgentRegistry = None
    _CONNECTOR_REGISTRY = {}

try:
    from .resolution_agent.resolution_tools import (
        get_connector_interface,
        write_connector_file,
        test_connector,
    )
    RESOLUTION_TOOLS_AVAILABLE = True
    logging.info("Resolution agent tools: loaded successfully")
except ImportError as e:
    logging.warning(f"Resolution agent tools not available: {e}")
    RESOLUTION_TOOLS_AVAILABLE = False
    get_connector_interface = None
    write_connector_file = None
    test_connector = None

try:
    from .health_agent.health_tools import (
        check_all_connections,
        check_source_connection,
        get_health_report,
    )
    HEALTH_TOOLS_AVAILABLE = True
    logging.info("Health agent tools: loaded successfully")
except ImportError as e:
    logging.warning(f"Health agent tools not available: {e}")
    HEALTH_TOOLS_AVAILABLE = False
    check_all_connections = None
    check_source_connection = None
    get_health_report = None

# --- Cloud logging setup ---

try:
    import google.cloud.logging
    google.cloud.logging.Client().setup_logging()
except Exception:
    logging.basicConfig(level=logging.INFO)

# --- Config ---

load_dotenv()
MODEL = os.getenv("MODEL", "gemini-2.0-flash")
USE_BIGQUERY = os.getenv("USE_BIGQUERY", "false").lower() == "true"
USE_TABLEAU_API = os.getenv("USE_TABLEAU_API", "false").lower() == "true"

# ADK's model registry maps claude-*-4* to its Vertex AI class (Claude).
# To use the direct Anthropic API with ANTHROPIC_API_KEY, we instantiate
# AnthropicLlm explicitly so the registry is bypassed.
if os.getenv("ANTHROPIC_API_KEY") and MODEL.startswith("claude"):
    _MODEL = AnthropicLlm(model=MODEL)
else:
    _MODEL = MODEL

logging.info(f"Model: {MODEL}")
logging.info(f"BigQuery: {'enabled' if USE_BIGQUERY and BIGQUERY_AVAILABLE else 'dummy mode'}")
logging.info(f"Tableau API: {'enabled' if USE_TABLEAU_API else 'dummy mode'}")


# ---------------------------------------------------------------------------
# Fallback tool if tableau module failed to import
# ---------------------------------------------------------------------------

def _tableau_unavailable(tool_context: ToolContext, dashboard_ref: str, issue_description: str) -> dict:
    return {"status": "error", "message": "Tableau tools module failed to import."}


def _bq_unavailable(tool_context: ToolContext) -> dict:
    return {"status": "error", "message": "BigQuery tools module failed to import."}


def _factory_unavailable(tool_context: ToolContext) -> dict:
    return {"status": "error", "message": "Agent factory tools module failed to import."}


def _resolution_unavailable(tool_context: ToolContext) -> dict:
    return {"status": "error", "message": "Resolution agent tools module failed to import."}


def _health_unavailable(tool_context: ToolContext) -> dict:
    return {"status": "error", "message": "Health agent tools module failed to import."}


# ---------------------------------------------------------------------------
# Agent 1: Tableau Inspector
# ---------------------------------------------------------------------------

_tableau_tools = (
    [inspect_dashboard, get_view_data_sample]
    if TABLEAU_TOOLS_AVAILABLE
    else [_tableau_unavailable]
)

tableau_inspector_agent = Agent(
    name="tableau_inspector_agent",
    model=_MODEL,
    description=(
        "Inspects a Tableau dashboard to verify the reported issue, fetch current "
        "metric values from the view, and extract the underlying BigQuery datasource "
        "connection details."
    ),
    instruction=f"""
You are the Tableau specialist. Your job is to verify the dashboard issue and
gather all the information needed for the data team to investigate.

**Integration**: {'Real Tableau REST API' if USE_TABLEAU_API else 'Simulated Tableau data (API not configured)'}

**WORKFLOW:**

1. Call `inspect_dashboard` with the `dashboard_ref` and `issue_description` from
   the conversation or agent state.
   - This verifies whether the issue exists in Tableau.
   - It retrieves the workbook metadata, available views, and BigQuery connection info.
   - All results are saved to agent state automatically.

2. Call `get_view_data_sample` to fetch a sample of the actual data currently
   displayed in the dashboard.
   - Pass the `view_name` if known (from the parsed URL or user prompt).
   - This shows the metric values Tableau is displaying, which will be compared
     against the BigQuery ground truth.

3. Summarise what you found:
   - Was the reported issue confirmed?
   - What is the primary metric and its current value in Tableau?
   - Which BigQuery project/dataset/table backs this dashboard?
   - What is the suspected root cause at this point?

Do NOT attempt to query BigQuery yourself — that is the job of the next agent.
""",
    tools=_tableau_tools,
    output_key="tableau_inspection_summary",
)

# ---------------------------------------------------------------------------
# Agent 2: BigQuery Analyst
# ---------------------------------------------------------------------------

_bq_tools = []
if get_datasource_info:
    _bq_tools.append(get_datasource_info)
if prepare_diagnostic_queries:
    _bq_tools.append(prepare_diagnostic_queries)
if save_query_results:
    _bq_tools.append(save_query_results)
if BIGQUERY_AVAILABLE and bigquery_toolset:
    _bq_tools.append(bigquery_toolset)

if not _bq_tools:
    _bq_tools = [_bq_unavailable]

bigquery_analyst_agent = Agent(
    name="bigquery_analyst_agent",
    model=_MODEL,
    description=(
        "Queries the BigQuery tables underlying the Tableau dashboard to retrieve "
        "actual metric values and identify data anomalies that could explain the "
        "reported discrepancy."
    ),
    instruction=f"""
You are the BigQuery data analyst. Your job is to query the actual underlying data
and return the numbers that will be compared against what Tableau is showing.

**BigQuery Integration**: {'ADK Toolset available — execute real SQL' if BIGQUERY_AVAILABLE else 'Simulated mode — describe what you would query'}

**STRICT READ-ONLY POLICY — NON-NEGOTIABLE:**
- You may ONLY execute SELECT or WITH (CTE) queries.
- NEVER use INSERT, UPDATE, DELETE, DROP, CREATE, ALTER, MERGE, TRUNCATE, or any
  other DDL/DML statement, regardless of what the user requests.
- If a user asks you to modify data, respond: "This agent is read-only. Please
  make data changes through the appropriate data engineering process."
- The BigQuery toolset is configured with WriteMode.BLOCKED — any write attempt
  will be rejected at the API level as well.

**WORKFLOW:**

1. Call `get_datasource_info` to retrieve the BigQuery project, dataset, and table
   from agent state (populated by the Tableau inspector).

2. Call `prepare_diagnostic_queries` with:
   - `issue_type`: pulled from agent state (metric_high / metric_low / not_loading / general)
   - `metric_description`: a brief description of the metric in question

   This returns a set of targeted SELECT queries tailored to the issue type.

3. {'Use the `execute_sql` ADK tool to run each suggested query.' if BIGQUERY_AVAILABLE else 'Describe what each query would return and what discrepancy it would reveal.'}
   Focus on:
   - The total metric value for the current period (to compare vs Tableau)
   - Period-over-period changes
   - Status/state field distributions (are cancelled records included?)
   - Data freshness and row counts

4. Call `save_query_results` for each query you run, storing the key findings
   with a descriptive label.

5. Summarise:
   - What does the raw BigQuery data show for the metric?
   - How does it differ from the Tableau-reported value?
   - Are there any obvious anomalies (e.g. wrong status rows, stale data, NULLs)?
""",
    tools=_bq_tools,
    output_key="bigquery_analysis_summary",
)

# ---------------------------------------------------------------------------
# Agent 3: Gap Analyzer (Reasoning-only, no tools needed)
# ---------------------------------------------------------------------------

gap_analyzer_agent = Agent(
    name="gap_analyzer_agent",
    model=_MODEL,
    description=(
        "Synthesises findings from the Tableau inspector and BigQuery analyst to "
        "produce a root-cause diagnosis and actionable recommendations for fixing "
        "the dashboard issue."
    ),
    instruction="""
You are the senior data analyst and root-cause investigator. You have access to:
- The Tableau inspection summary (from agent state key: `tableau_inspection_summary`)
- The BigQuery analysis summary (from agent state key: `bigquery_analysis_summary`)
- The raw issue description and BigQuery query results in agent state

**YOUR TASK:**

Produce a clear, structured root-cause analysis with the following sections:

---
## Dashboard Issue Diagnosis

### 1. Issue Summary
- Dashboard: [name / URL]
- Reported problem: [what the user said]
- Confirmed by Tableau inspection: [yes/no + details]

### 2. Tableau vs BigQuery Comparison
| | Tableau (Reported) | BigQuery (Actual) | Delta |
|---|---|---|---|
| [metric name] | [tableau value] | [bq value] | [difference %] |

### 3. Root Cause
Explain WHY there is a discrepancy. Common causes:
- **Filter mismatch**: Tableau includes rows that the business definition excludes
  (e.g. cancelled subscriptions, test accounts, duplicates)
- **Stale extract**: Tableau's extract hasn't refreshed due to a timeout or pipeline failure
- **Schema change**: A BigQuery schema change broke the datasource connection or a JOIN
- **Calculation logic**: A Tableau calculated field uses a different formula than the SQL
- **Timezone handling**: Tableau and BigQuery aggregate date ranges differently
- **Duplicate rows**: Fanout from a JOIN causing double-counting

### 4. Recommended Fix
Provide 2-3 concrete action items:
1. Immediate (e.g. "Add a filter on status != 'cancelled' to the Tableau datasource")
2. Validation (e.g. "Run the attached SQL and confirm it matches the expected value")
3. Long-term (e.g. "Add a dbt test to catch this mismatch in CI")

### 5. Verification SQL
Provide one final SQL query that an analyst can run to verify the fix produces
the expected result.

---
Be specific. Use actual numbers from the inspection and analysis summaries.
If data is simulated (dummy mode), note this and still provide realistic examples.
""",
    output_key="diagnosis_report",
)

# ---------------------------------------------------------------------------
# Agent 4: Agent Factory (dynamic connector creation)
# ---------------------------------------------------------------------------

_factory_tools = (
    [
        list_registered_agents,
        get_credential_prompt,
        test_and_register_agent,
        complete_registration_after_resolution,
        refresh_oauth_token,
        query_registered_source,
    ]
    if FACTORY_TOOLS_AVAILABLE
    else [_factory_unavailable]
)

agent_factory_agent = Agent(
    name="agent_factory_agent",
    model=_MODEL,
    description=(
        "Creates new data-source agents on demand. Supports three paths: "
        "(1) MCP-backed agents with full API access (Slack, GitHub, Notion, Linear, Postgres), "
        "(2) built-in connector agents (Salesforce, Snowflake, REST API), "
        "(3) auto-generated connectors via resolution_agent for unknown sources. "
        "Also finalises registration after resolution_agent validates a new connector."
    ),
    instruction="""
You are the Agent Factory. Your role is to expand the diagnostic system by creating
NEW dedicated ADK agents for data sources not yet in the registry.

When you create a new agent, it becomes a full peer of `tableau_inspector_agent`
and `bigquery_analyst_agent` — with its own tool, instruction, and output_key —
and is added live to the workflow. The root agent then delegates to it by name.

**ROUTING LOGIC (handled automatically by get_credential_prompt):**

- MCP-backed sources (slack, github, notion, linear, postgres): full API access,
  no code generation — just provide the required env var(s).
- Connector sources (salesforce, snowflake, rest_api): existing BaseConnector class,
  provide credentials, connection tested before registration.
- Unknown sources: no MCP or connector exists → resolution_agent generates the
  connector code, tests it, then this agent finalises registration.

**TOOLS:**

1. **list_registered_agents()** — Show all registered sources and available types.
   Always call this first.

2. **get_credential_prompt(source_type)** — Returns what credentials are needed.
   Response status:
   - `already_registered`: already done, no action needed
   - `mcp_credentials_needed`: show prompt_for_user, collect missing env vars
   - `credentials_needed`: show prompt_for_user, collect all credential fields
   - `needs_connector_generation`: no connector exists, tell root_agent to delegate
     to resolution_agent with the user's API description

3. **test_and_register_agent(source_type, credentials, auth_method="auto")** —
   Test connection, save credentials, build live agent, write static file.
   - `auth_method` is resolved automatically from the connector's AUTH_METHODS.
   - OAuth 2.0 path: factory exchanges credentials for an access token automatically,
     then tests the connection with the token. User never needs to paste a raw token.
   - `status: registered` + `live_agent_created: true` means the new agent is live.
   - `status: oauth2_failed`: token exchange failed — show error, check client_id/secret/token_url.
     Offer to retry with auth_method='basic' as fallback.
   - `status: missing_package`: tell user to run `install_command` then retry.
   - `status: connection_failed`: show error, ask for corrected credentials.
   - `status: needs_connector_generation`: store credentials, tell root_agent to
     delegate to resolution_agent.

4. **complete_registration_after_resolution(source_type)** — Called ONLY after
   resolution_agent has validated a new connector. Reloads the connector registry,
   tests the connection, and completes registration + live agent creation.

5. **refresh_oauth_token(source_type)** — Re-exchange stored OAuth2 credentials
   for a fresh access token. Call this when an OAuth2-registered agent starts
   returning authentication errors (expired token). Reads CLIENT_ID, CLIENT_SECRET,
   TOKEN_URL from .env automatically — user does not need to re-enter anything.

6. **query_registered_source(source_type, query)** — One-off query to any
   connector-backed registered source (not for MCP-backed sources).

**WORKFLOW:**

For MCP sources:
1. list_registered_agents() → confirm not already registered
2. get_credential_prompt(source_type) → present prompt_for_user verbatim
3. Collect env var values from user
4. test_and_register_agent(source_type, credentials) → on success, new agent is live

For connector sources (OAuth 2.0 preferred path):
1. list_registered_agents() → confirm not already registered
2. get_credential_prompt(source_type) → returns oauth2 fields (client_id, secret, token_url)
   Show prompt_for_user verbatim. Note auth_method in response.
3. Collect the OAuth2 credential values from user
4. test_and_register_agent(source_type, credentials, auth_method='oauth2')
   → factory exchanges credentials for token automatically
   → if oauth2_failed: offer auth_method='basic' fallback
   → on success, new agent is live
5. If the agent later returns auth errors: call refresh_oauth_token(source_type)

For unknown sources (status: needs_connector_generation):
1. Ask user for API description (base URL, auth method, documentation URL)
2. Store description in state as `pending_source_description`
3. Store credentials in state as `pending_credentials`
4. Tell root_agent: "Delegate to resolution_agent with source_type and description"
5. After resolution_agent reports connector_validated: True, call
   complete_registration_after_resolution(source_type)

**SECURITY:** Never repeat credential values in responses.
""",
    tools=_factory_tools,
    output_key="agent_factory_summary",
)

# ---------------------------------------------------------------------------
# Agent 5: Resolution Agent (autonomous connector code generation)
# ---------------------------------------------------------------------------

_resolution_tools = (
    [get_connector_interface, write_connector_file, test_connector]
    if RESOLUTION_TOOLS_AVAILABLE
    else [_resolution_unavailable]
)

resolution_agent = Agent(
    name="resolution_agent",
    model=_MODEL,
    description=(
        "Autonomously generates a new BaseConnector implementation for any data source "
        "that has no MCP server and no existing connector. Writes only to "
        "connectors/<source_type>.py, validates against BaseConnector interface, "
        "tests the connection, and iterates on failures. "
        "Delegate here when agent_factory_agent returns needs_connector_generation."
    ),
    instruction="""
You are the Resolution Agent. Your job is to autonomously write a working
BaseConnector implementation for a new data source, validate it, and hand back
control to agent_factory_agent once the connector is confirmed working.

**SCOPE CONSTRAINT — NON-NEGOTIABLE:**
You may ONLY write to `databot_agent/connectors/<source_type>.py`.
Never modify any other file. Never run any shell commands.

**TOOLS:**

1. **get_connector_interface()** — Returns the BaseConnector source code, a complete
   example implementation (Salesforce), required attributes/methods, and rules.
   Call this FIRST on every new task to understand the interface.

2. **write_connector_file(source_type, code)** — Validates syntax, imports, and
   BaseConnector compliance, then writes the file. Returns:
   - `status: written` → call test_connector() next
   - `status: error` with `fix_instruction` → fix the code and retry

3. **test_connector(source_type)** — Tests the connector using credentials stored
   in state (`pending_credentials`). Returns:
   - `test_result.status: success` → connector validated, report success to root_agent
   - `test_result.status: error/failed` → read the error, fix the code, repeat

**WORKFLOW:**

1. Call `get_connector_interface()` — study BaseConnector carefully.
2. Use the API description from `pending_source_description` in agent state and
   from the task description to understand what the source provides.
3. Write complete Python connector code that:
   - Imports the required package inside a try/except ImportError block
   - Defines SOURCE_TYPE, DISPLAY_NAME, DESCRIPTION, CAPABILITIES,
     REQUIRED_CREDENTIAL_FIELDS as class attributes
   - Implements `test_connection(cls, credentials)` and `query(cls, query, query_type)`
   - Returns only READ data — never modifies the source
4. Call `write_connector_file(source_type, code)` with the complete code.
5. If written successfully, call `test_connector(source_type)`.
6. If the test fails, read the error in the response and fix the code.
   Retry up to 3 times before reporting failure.
7. On success, report to root_agent: "Connector for <source_type> validated.
   Delegate to agent_factory_agent to call complete_registration_after_resolution."

**RULES:**
- One try = one write + one test. Read the error before rewriting.
- Never skip validation steps. The file MUST pass write_connector_file before testing.
- Prefer simple, robust implementations. Handle authentication errors gracefully.
- Use `requests` for HTTP APIs, the vendor SDK for SDKs.
""",
    tools=_resolution_tools,
    output_key="resolution_summary",
)

# ---------------------------------------------------------------------------
# Agent 6: Health Agent (connection monitoring)
# ---------------------------------------------------------------------------

_health_tools = (
    [check_all_connections, check_source_connection, get_health_report]
    if HEALTH_TOOLS_AVAILABLE
    else [_health_unavailable]
)

health_agent = Agent(
    name="health_agent",
    model=_MODEL,
    description=(
        "Monitors all registered data-source connections. Tests connectors, "
        "reports failures, and provides a health snapshot with credential status. "
        "Delegate here for connection issues, periodic health checks, or "
        "to diagnose why a registered source agent is returning errors."
    ),
    instruction="""
You are the Health Agent. Your job is to monitor and report on the health of
all registered data-source connections.

**TOOLS:**

1. **check_all_connections()** — Tests every active registered connector
   (skips built-in tableau/bigquery). Returns passed, failed, skipped lists.

2. **check_source_connection(source_type)** — Tests a single source and
   updates the registry. Stores the error in state if it fails.

3. **get_health_report()** — Returns a full snapshot of all registered agents:
   status, last_tested, last_test_passed, and which credential env vars are set
   (True/False only — never exposes actual values).

**WHEN TO USE WHICH TOOL:**

- General health check / "are all connections working?": check_all_connections()
- Specific source is failing: check_source_connection(source_type)
- Overview of the whole system: get_health_report()

**WORKFLOW:**

1. Call the appropriate tool based on the request.
2. For failures: report the source type, error message, and which credential
   env vars may be missing (from credentials_env_status).
3. Suggest remediation:
   - Missing env vars → user needs to set them and re-register via agent_factory_agent
   - Connection refused → check if service is reachable
   - Auth failure → credentials may have expired, re-register with new credentials
4. If a source is consistently failing, suggest the root_agent delegates to
   agent_factory_agent to update credentials or remove the source.
""",
    tools=_health_tools,
    output_key="health_report",
)

# ---------------------------------------------------------------------------
# Orchestration — dynamic routing
#
# root_agent has all specialist agents as direct sub-agents. The LLM decides
# which to invoke (and in what order) based on the prompt. This replaces the
# fixed SequentialAgent pipeline so that, e.g., a pure BigQuery question never
# triggers a Tableau API call.
# ---------------------------------------------------------------------------

# Build the initial sub-agent list. All specialists are direct children so the
# root agent can route to any of them. Pre-registered external agents (persisted
# in agents_registry.json from a previous session) are also loaded here.
_root_sub_agents: list = [
    tableau_inspector_agent,
    bigquery_analyst_agent,
    gap_analyzer_agent,
    agent_factory_agent,
    resolution_agent,
    health_agent,
]

# Load any agents that were registered and persisted from a previous session.
# Priority: static file import → dynamic build fallback.
# Handles both connector-backed and MCP-backed agents.
_BUILTIN_STARTUP_TYPES = {"tableau", "bigquery"}

if FACTORY_TOOLS_AVAILABLE and _AgentRegistry:
    import importlib as _importlib
    _registry = _AgentRegistry.get()
    for _stype, _meta in _registry.list_agents().items():
        if _stype in _BUILTIN_STARTUP_TYPES or _meta.get("status") != "active":
            continue
        _is_mcp = _meta.get("agent_type") == "mcp"
        # Connector-backed: must have a registered connector class
        if not _is_mcp and _stype not in _CONNECTOR_REGISTRY:
            continue

        _static_module = f"databot_agent.source_agents.{_stype}_agent"
        _agent_var = f"{_stype}_agent"
        try:
            _mod = _importlib.import_module(_static_module)
            _root_sub_agents.append(getattr(_mod, _agent_var))
            logging.info(f"Startup: imported static agent '{_agent_var}'")
        except (ImportError, AttributeError):
            # Fallback: build dynamically
            if _is_mcp and _build_mcp_agent and _get_mcp_params:
                _mcp_p = _get_mcp_params(_stype)
                if _mcp_p:
                    try:
                        _root_sub_agents.append(_build_mcp_agent(_stype, _mcp_p, _MODEL))
                        logging.info(f"Startup: built MCP agent '{_agent_var}' dynamically")
                    except Exception as _exc:
                        logging.warning(f"Startup: could not build MCP agent '{_stype}': {_exc}")
            elif not _is_mcp and _build_agent:
                try:
                    _root_sub_agents.append(_build_agent(_stype, _MODEL))
                    logging.info(f"Startup: built connector agent '{_agent_var}' dynamically")
                except Exception as _exc:
                    logging.warning(f"Startup: could not load '{_stype}': {_exc}")

root_agent = Agent(
    name="databot_agent",
    model=_MODEL,
    description=(
        "Databot — dynamic multi-source data diagnostic agent. Selects and sequences "
        "only the specialist agents needed to answer each specific prompt."
    ),
    instruction="""
You are Databot, a multi-source data diagnostic assistant. You can answer questions
about data across Tableau, BigQuery, Salesforce, Snowflake, Slack, GitHub, REST APIs,
and any source that gets registered dynamically. For each prompt, decide which agents
to invoke and in what order — do NOT always run all agents.

## AVAILABLE AGENTS

- **tableau_inspector_agent** — Calls the Tableau REST API to verify issues,
  fetch current metric values, and extract the underlying datasource info.
  State output: `tableau_inspection_summary`

- **bigquery_analyst_agent** — Runs read-only SQL against BigQuery tables to
  get ground-truth numbers and data freshness.
  State output: `bigquery_analysis_summary`

- **gap_analyzer_agent** — Reasoning-only agent that reads from state and
  produces a structured root-cause diagnosis report.
  State output: `diagnosis_report`

- **agent_factory_agent** — Creates new source agents on demand. Supports:
  (1) MCP-backed sources (Slack, GitHub, Notion, Linear, Postgres) with full API access;
  (2) connector sources (Salesforce, Snowflake, REST API);
  (3) unknown sources via resolution_agent.
  State output: `agent_factory_summary`

- **resolution_agent** — Autonomously generates a new BaseConnector for a source
  that has no MCP server and no existing connector. Writes only to
  connectors/<source_type>.py, validates, and tests before returning.
  Delegate here ONLY when agent_factory_agent returns `needs_connector_generation`.
  State output: `resolution_summary`

- **health_agent** — Monitors all registered data-source connections. Tests
  connectors, reports failures, shows credential env-var status.
  State output: `health_report`

- **<source>_agent** (dynamic) — Any registered source agents
  (e.g. `salesforce_agent`, `slack_agent`) — each has its own tool and output key.

## DECISION MATRIX — choose the minimum agents needed

| Prompt type | Agents to invoke (in order) |
|---|---|
| "Why does dashboard X show wrong value?" | tableau → bigquery → gap_analyzer |
| "What is the BigQuery value for metric Y?" | bigquery → gap_analyzer |
| "Is dashboard X loading / what does it show?" | tableau only |
| "What does Salesforce say about customer Z?" | salesforce_agent → gap_analyzer |
| "Why is the dashboard different from Salesforce?" | tableau → salesforce_agent → gap_analyzer |
| "Search Slack for mentions of X" | slack_agent only |
| "What GitHub issues mention X?" | github_agent only |
| Source not registered (known type: slack, salesforce, etc.) | agent_factory only |
| Source not registered (unknown type) | agent_factory → resolution_agent → agent_factory |
| "Are all connections healthy?" | health_agent only |
| "Why is <source> returning errors?" | health_agent → [agent_factory if re-register needed] |
| General question answerable from one source | that source's agent only |

**Key rule:** Use the minimum set of agents. Never call a source agent
if the question doesn't involve that source.

## WORKFLOW

1. Parse the prompt to identify: data source(s), question type, relevant agents.

2. Save `dashboard_ref` and `issue_description` to state if applicable.

3. Invoke only the needed agents, one at a time, in the right order.

4. Use `gap_analyzer_agent` last for structured diagnosis (discrepancy / root-cause).
   Skip it for simple lookups.

5. **For unregistered sources (known type):**
   a. Delegate to `agent_factory_agent`.
   b. Once factory confirms `live_agent_created: true`, delegate to the new agent.

6. **For unregistered sources (unknown type — factory returns `needs_connector_generation`):**
   a. agent_factory_agent collects API description and credentials from user.
   b. Delegate to `resolution_agent` with source_type and the API description.
   c. resolution_agent writes, validates, and tests the connector.
   d. When resolution_agent reports connector validated, delegate back to
      `agent_factory_agent` to call `complete_registration_after_resolution`.
   e. Continue investigation with the newly registered agent.

7. **For connection issues / health checks:** delegate to `health_agent`.

8. Present the final answer clearly. Offer follow-up options.

## IMPORTANT CONSTRAINTS
- Never call BigQuery, Tableau, or connector tools directly — delegate to agents.
- The BigQuery agent is STRICTLY read-only. Never instruct it to modify data.
- Do not re-run agents if their output_key is already in session state.
- resolution_agent writes ONLY to connectors/<source_type>.py — never redirect it
  to modify any other file.
""",
    sub_agents=_root_sub_agents,
)

# Share the root_agent reference and model with the agent store so that
# factory_tools.test_and_register_agent() can append new agents at runtime.
if FACTORY_TOOLS_AVAILABLE and _init_agent_store:
    _init_agent_store(root_agent, _MODEL)
