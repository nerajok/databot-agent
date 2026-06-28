"""
BigQuery tools for Databot Agent.

Uses Google ADK's first-party BigQuery toolset (execute_sql, get_table_info, etc.)
plus helper functions that prepare diagnostic SQL based on the dashboard issue type.
"""

import logging
import os

import google.auth
from google.adk.integrations.bigquery import BigQueryCredentialsConfig, BigQueryToolset
from google.adk.integrations.bigquery.config import BigQueryToolConfig, WriteMode
from google.adk.tools.tool_context import ToolContext

PROJECT_ID = os.getenv("GOOGLE_CLOUD_PROJECT", "dialpad-data-team")

# DDL/DML keywords that must never appear in agent-generated queries.
_FORBIDDEN_SQL_KEYWORDS = frozenset([
    "insert", "update", "delete", "drop", "truncate", "create", "alter",
    "merge", "replace", "grant", "revoke", "call", "execute",
])


def _assert_read_only(sql: str) -> None:
    """Raise ValueError if the SQL contains any write/DDL keyword."""
    first_word = sql.strip().split()[0].lower() if sql.strip() else ""
    if first_word != "select" and first_word != "with":
        raise ValueError(
            f"Only SELECT/WITH queries are permitted. Got keyword: '{first_word}'"
        )
    tokens = set(sql.lower().split())
    violations = _FORBIDDEN_SQL_KEYWORDS & tokens
    if violations:
        raise ValueError(
            f"Query contains forbidden keyword(s): {violations}. "
            "Only read-only SELECT queries are allowed."
        )


def get_bigquery_toolset() -> BigQueryToolset:
    """
    Initialise the ADK BigQuery toolset using Application Default Credentials.
    Returns None on failure so callers can fall back to dummy mode.
    """
    try:
        tool_config = BigQueryToolConfig(write_mode=WriteMode.BLOCKED)
        credentials, _ = google.auth.default()
        credentials_config = BigQueryCredentialsConfig(credentials=credentials)
        toolset = BigQueryToolset(
            credentials_config=credentials_config,
            bigquery_tool_config=tool_config,
        )
        logging.info("ADK BigQuery toolset initialised successfully")
        return toolset
    except Exception as e:
        logging.error(f"Failed to initialise BigQuery toolset: {e}")
        return None


# ---------------------------------------------------------------------------
# Helper tool functions exposed to the ADK agent
# ---------------------------------------------------------------------------


def get_datasource_info(tool_context: ToolContext) -> dict:
    """
    Retrieve the BigQuery connection details discovered by the Tableau inspector
    agent from shared agent state.

    Returns project, dataset, and table information so the analyst agent knows
    which BigQuery resources to query.
    """
    connections = tool_context.state.get("datasource_connections", [])
    if not connections:
        return {
            "status": "no_connections",
            "message": "No datasource connections found in state. Run the Tableau inspector first.",
        }

    bq_connections = [c for c in connections if c.get("type", "").lower() in ("bigquery", "google-bigquery")]

    if not bq_connections:
        logging.warning("No BigQuery connections found; returning all connections for reference")
        bq_connections = connections

    parsed = []
    for conn in bq_connections:
        raw_db = conn.get("db", "") or conn.get("dbName", "")
        # Tableau sometimes stores "project.dataset" in the db field
        if "." in raw_db:
            project, dataset = raw_db.split(".", 1)
        else:
            project = raw_db or PROJECT_ID
            dataset = conn.get("schema", "") or conn.get("schema_name", "")

        table = conn.get("table", "") or conn.get("tableName", "")
        parsed.append(
            {
                "project": project or PROJECT_ID,
                "dataset": dataset,
                "table": table,
                "raw_connection": conn,
            }
        )

    tool_context.state["bigquery_connections"] = parsed
    return {
        "status": "success",
        "bigquery_connections": parsed,
        "primary": parsed[0] if parsed else None,
    }


def prepare_diagnostic_queries(
    tool_context: ToolContext,
    issue_type: str = "",
    metric_description: str = "",
) -> dict:
    """
    Generate SQL query templates for diagnosing a specific dashboard issue.
    The analyst agent should execute these using the 'execute_sql' ADK tool.

    Args:
        issue_type: One of 'metric_high', 'metric_low', 'not_loading', 'general'.
        metric_description: Free-text description of the metric or field in question.

    Returns:
        dict with a list of suggested SQL queries and guidance notes.
    """
    resolved_issue = issue_type or tool_context.state.get("issue_type", "general")
    connections = tool_context.state.get("bigquery_connections", [])
    tableau_view_data = tool_context.state.get("tableau_view_data", {})

    if not connections:
        return {
            "status": "no_connections",
            "message": "Call get_datasource_info first to resolve the BigQuery connection.",
        }

    primary = connections[0]
    project = primary["project"]
    dataset = primary["dataset"]
    table = primary["table"]
    full_table = f"`{project}.{dataset}.{table}`" if table else f"`{project}.{dataset}.*`"

    tableau_value = tableau_view_data.get("current_value", "N/A")
    period = tableau_view_data.get("period", "")
    current_month = period or "FORMAT_DATE('%Y-%m', CURRENT_DATE())"

    queries = []

    # --- Always-useful baseline query: row count and date range ---
    queries.append(
        {
            "label": "baseline_summary",
            "description": "Get row count, date range, and a sense of data freshness.",
            "sql": f"""
SELECT
  COUNT(*) AS total_rows,
  MIN(created_at) AS earliest_record,
  MAX(created_at) AS latest_record,
  COUNTIF(DATE(created_at) = CURRENT_DATE()) AS rows_today
FROM {full_table}
""".strip(),
        }
    )

    if resolved_issue in ("metric_high", "metric_low"):
        # Period-over-period comparison for revenue-style metrics
        queries.append(
            {
                "label": "period_comparison",
                "description": (
                    f"Compare the current period vs prior period for the metric. "
                    f"Tableau is showing {tableau_value} — verify against actual data."
                ),
                "sql": f"""
WITH monthly AS (
  SELECT
    FORMAT_DATE('%Y-%m', DATE(created_at)) AS month,
    SUM(amount) AS total_amount,
    COUNT(DISTINCT account_id) AS account_count
  FROM {full_table}
  WHERE created_at >= DATE_SUB(CURRENT_DATE(), INTERVAL 90 DAY)
  GROUP BY 1
)
SELECT
  month,
  total_amount,
  account_count,
  LAG(total_amount) OVER (ORDER BY month) AS prior_month_amount,
  SAFE_DIVIDE(total_amount - LAG(total_amount) OVER (ORDER BY month),
               LAG(total_amount) OVER (ORDER BY month)) * 100 AS pct_change
FROM monthly
ORDER BY month DESC
LIMIT 6
""".strip(),
            }
        )

        # Check for unusual status/state values that might inflate/deflate numbers
        queries.append(
            {
                "label": "status_distribution",
                "description": (
                    "Check distribution of status/state fields. "
                    "Tableau may be including rows that should be excluded (e.g. cancelled subscriptions)."
                ),
                "sql": f"""
SELECT
  status,
  COUNT(*) AS row_count,
  SUM(amount) AS total_amount
FROM {full_table}
WHERE FORMAT_DATE('%Y-%m', DATE(created_at)) = FORMAT_DATE('%Y-%m', CURRENT_DATE())
GROUP BY 1
ORDER BY total_amount DESC
""".strip(),
            }
        )

        if resolved_issue == "metric_high":
            queries.append(
                {
                    "label": "outlier_detection",
                    "description": "Identify accounts with unusually large values that might be inflating the total.",
                    "sql": f"""
SELECT
  account_id,
  SUM(amount) AS total_amount,
  COUNT(*) AS transaction_count
FROM {full_table}
WHERE FORMAT_DATE('%Y-%m', DATE(created_at)) = FORMAT_DATE('%Y-%m', CURRENT_DATE())
GROUP BY 1
HAVING total_amount > (
  SELECT APPROX_QUANTILES(amount, 100)[OFFSET(95)]
  FROM {full_table}
  WHERE FORMAT_DATE('%Y-%m', DATE(created_at)) = FORMAT_DATE('%Y-%m', CURRENT_DATE())
)
ORDER BY total_amount DESC
LIMIT 20
""".strip(),
                }
            )

        if resolved_issue == "metric_low":
            queries.append(
                {
                    "label": "missing_data_check",
                    "description": "Check for NULL amounts or accounts missing from the current period.",
                    "sql": f"""
SELECT
  COUNTIF(amount IS NULL) AS null_amount_count,
  COUNTIF(account_id IS NULL) AS null_account_count,
  COUNTIF(created_at IS NULL) AS null_date_count,
  COUNT(*) AS total_rows
FROM {full_table}
WHERE FORMAT_DATE('%Y-%m', DATE(created_at)) = FORMAT_DATE('%Y-%m', CURRENT_DATE())
""".strip(),
                }
            )

    elif resolved_issue == "not_loading":
        queries.append(
            {
                "label": "data_freshness",
                "description": "Check the most recent data available in BigQuery.",
                "sql": f"""
SELECT
  MAX(created_at) AS latest_data,
  TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), MAX(created_at), HOUR) AS hours_since_last_update,
  COUNT(*) AS rows_in_last_24h
FROM {full_table}
WHERE created_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 24 HOUR)
""".strip(),
            }
        )

        queries.append(
            {
                "label": "table_size_check",
                "description": "Estimate table size — large unpartitioned tables often cause extract timeouts.",
                "sql": f"""
SELECT
  table_id,
  ROUND(size_bytes / POW(1024, 3), 2) AS size_gb,
  row_count,
  DATE(TIMESTAMP_MILLIS(last_modified_time)) AS last_modified
FROM `{project}.{dataset}.__TABLES__`
WHERE table_id = '{table}'
""".strip(),
            }
        )

    else:  # general
        queries.append(
            {
                "label": "schema_exploration",
                "description": "Sample the table to understand its structure and find the relevant metric fields.",
                "sql": f"""
SELECT *
FROM {full_table}
ORDER BY created_at DESC
LIMIT 10
""".strip(),
            }
        )

    # Guard: every generated query must be read-only before we surface it.
    for q in queries:
        try:
            _assert_read_only(q["sql"])
        except ValueError as e:
            logging.error(f"Read-only violation in generated query '{q['label']}': {e}")
            raise

    tool_context.state["diagnostic_queries"] = queries
    return {
        "status": "success",
        "issue_type": resolved_issue,
        "target_table": full_table,
        "tableau_reported_value": tableau_value,
        "queries": queries,
        "instructions": (
            "Execute each query using the 'execute_sql' tool. "
            "Save the results to compare against the Tableau-reported value. "
            "Look for discrepancies in totals, unexpected status distributions, "
            "or missing rows that explain the dashboard issue."
        ),
    }


def save_query_results(
    tool_context: ToolContext,
    query_label: str,
    results_summary: str,
) -> dict:
    """
    Persist a summary of BigQuery query results to agent state so the
    Gap Analyzer agent can access them.

    Args:
        query_label: The label identifying which diagnostic query was run.
        results_summary: A plain-text or JSON summary of the query output.

    Returns:
        Confirmation dict.
    """
    existing = tool_context.state.get("bigquery_results", {})
    existing[query_label] = results_summary
    tool_context.state["bigquery_results"] = existing
    logging.info(f"BigQuery: Saved results for query '{query_label}'")
    return {"status": "saved", "query_label": query_label}
