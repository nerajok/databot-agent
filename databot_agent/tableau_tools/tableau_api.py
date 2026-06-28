"""
Tableau REST API client and ADK tool functions for dashboard inspection.

Supports real Tableau REST API (when credentials are set) and a dummy mode
for development/testing that simulates realistic dashboard issue scenarios.
"""

import logging
import os
import re
from typing import Optional

import requests
from dotenv import load_dotenv
from google.adk.tools.tool_context import ToolContext

load_dotenv()

TABLEAU_SERVER = os.getenv("TABLEAU_SERVER_URL", "")
TABLEAU_SITE = os.getenv("TABLEAU_SITE_NAME", "")
TABLEAU_TOKEN_NAME = os.getenv("TABLEAU_TOKEN_NAME", "")
TABLEAU_TOKEN_SECRET = os.getenv("TABLEAU_TOKEN_SECRET", "")
TABLEAU_API_VERSION = os.getenv("TABLEAU_API_VERSION", "3.19")

# --- Realistic dummy data for development/testing ---

DUMMY_DASHBOARDS = {
    "revenue-overview": {
        "workbook_id": "wb-001",
        "workbook_name": "Revenue Overview",
        "project_name": "Finance",
        "views": [
            {
                "id": "view-001",
                "name": "Monthly Revenue",
                "content_url": "RevenueOverview/Monthly_Revenue",
                "owner": "analytics@dialpad.com",
            },
            {
                "id": "view-002",
                "name": "Revenue by Region",
                "content_url": "RevenueOverview/Revenue_by_Region",
                "owner": "analytics@dialpad.com",
            },
        ],
        "connections": [
            {
                "type": "bigquery",
                "server": "bigquery.googleapis.com",
                "db": "dialpad-data-team",
                "schema": "revenue_analytics",
                "table": "subscription_revenue",
                "username": "service-account@dialpad-data-team.iam.gserviceaccount.com",
            }
        ],
        "last_refresh": "2026-06-23T04:00:00Z",
        "refresh_status": "success",
        "view_data": {
            "Monthly Revenue": {
                "metric": "total_revenue",
                "current_value": 45_000_000,
                "unit": "USD",
                "period": "2026-06",
                "sample_rows": [
                    {"month": "2026-06", "revenue": 45000000, "region": "All"},
                    {"month": "2026-05", "revenue": 30000000, "region": "All"},
                ],
            }
        },
    },
    "churn-dashboard": {
        "workbook_id": "wb-002",
        "workbook_name": "Churn Dashboard",
        "project_name": "Revenue Ops",
        "views": [
            {
                "id": "view-003",
                "name": "Monthly Churn Rate",
                "content_url": "ChurnDashboard/Monthly_Churn_Rate",
                "owner": "revenue-ops@dialpad.com",
            }
        ],
        "connections": [
            {
                "type": "bigquery",
                "server": "bigquery.googleapis.com",
                "db": "dialpad-data-team",
                "schema": "revenue_analytics",
                "table": "customer_churn",
                "username": "service-account@dialpad-data-team.iam.gserviceaccount.com",
            }
        ],
        "last_refresh": "2026-06-22T00:00:00Z",
        "refresh_status": "failed",
        "refresh_error": "BigQuery job timed out after 3600s",
        "view_data": {},
    },
}

DUMMY_ISSUE_CONTEXTS = {
    "metric_high": {
        "issue_verified": True,
        "verification_note": (
            "Dashboard confirms the reported value is higher than expected. "
            "Tableau is showing $45M for June 2026, compared to $30M in May 2026 "
            "(50% MoM increase). The underlying data source may include cancelled "
            "subscriptions that were not properly filtered."
        ),
        "suspected_cause": "Data source filter misconfiguration or inclusion of churned ARR",
    },
    "metric_low": {
        "issue_verified": True,
        "verification_note": (
            "Dashboard confirms the reported value is lower than expected. "
            "A recent schema change in BigQuery may have broken the JOIN or "
            "caused a field mismatch resulting in missing rows."
        ),
        "suspected_cause": "Missing data rows due to broken JOIN or schema change",
    },
    "not_loading": {
        "issue_verified": True,
        "verification_note": (
            "Dashboard extract refresh failed on 2026-06-22. "
            "The BigQuery job timed out after 3600 seconds. "
            "The dashboard is displaying stale data from 2026-06-21."
        ),
        "suspected_cause": "BigQuery extract refresh timeout — likely a large query or missing partition filter",
    },
    "general": {
        "issue_verified": True,
        "verification_note": (
            "Dashboard was accessed and basic metadata retrieved. "
            "No obvious loading errors detected."
        ),
        "suspected_cause": "Unknown — deeper investigation required",
    },
}


# --- Tableau REST API client ---


class TableauClient:
    """Tableau REST API client using Personal Access Token authentication."""

    def __init__(self):
        self.server = TABLEAU_SERVER.rstrip("/")
        self.site_name = TABLEAU_SITE
        self.token_name = TABLEAU_TOKEN_NAME
        self.token_secret = TABLEAU_TOKEN_SECRET
        self.api_version = TABLEAU_API_VERSION
        self.auth_token: Optional[str] = None
        self.site_id: Optional[str] = None
        self.base_url = f"{self.server}/api/{self.api_version}"

    def is_configured(self) -> bool:
        return all([self.server, self.token_name, self.token_secret])

    def sign_in(self) -> bool:
        if not self.is_configured():
            logging.warning("Tableau credentials not configured — using dummy mode")
            return False
        payload = {
            "credentials": {
                "personalAccessTokenName": self.token_name,
                "personalAccessTokenSecret": self.token_secret,
                "site": {"contentUrl": self.site_name},
            }
        }
        try:
            resp = requests.post(
                f"{self.base_url}/auth/signin",
                json=payload,
                headers={"Accept": "application/json"},
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()["credentials"]
            self.auth_token = data["token"]
            self.site_id = data["site"]["id"]
            logging.info("Tableau: Authenticated successfully")
            return True
        except Exception as e:
            logging.error(f"Tableau auth failed: {e}")
            return False

    @property
    def _headers(self) -> dict:
        return {
            "X-Tableau-Auth": self.auth_token or "",
            "Accept": "application/json",
        }

    def _get(self, path: str, params: Optional[dict] = None) -> Optional[dict]:
        try:
            resp = requests.get(
                f"{self.base_url}/sites/{self.site_id}/{path}",
                headers=self._headers,
                params=params or {},
                timeout=30,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logging.error(f"Tableau GET /{path} failed: {e}")
            return None

    def find_workbook(self, name: str) -> Optional[dict]:
        # Try display name first, then URL contentUrl slug (e.g. "NewPLG" → "[Finance] PLG Tracker")
        for filter_key in (f"name:eq:{name}", f"contentUrl:eq:{name}"):
            data = self._get("workbooks", {"filter": filter_key, "pageSize": 5})
            if data:
                items = data.get("workbooks", {}).get("workbook", [])
                if items:
                    return items[0]
        return None

    def get_views(self, workbook_id: str) -> list:
        data = self._get(f"workbooks/{workbook_id}/views")
        return data.get("views", {}).get("view", []) if data else []

    def find_view(self, views: list, hint: str) -> Optional[dict]:
        """
        Match a view by display name or URL slug.
        Strips punctuation/spaces so 'CDAdjustment' matches 'C&D Adjustment'.
        """
        import re
        def normalise(s: str) -> str:
            return re.sub(r"[^a-z0-9]", "", s.lower())

        hint_n = normalise(hint)
        for v in views:
            name_n = normalise(v.get("name", ""))
            url_n  = normalise(v.get("contentUrl", "").split("/")[-1])
            if hint_n in (name_n, url_n) or hint_n in name_n or hint_n in url_n:
                return v
        return views[0] if views else None

    def get_connections(self, workbook_id: str) -> list:
        data = self._get(f"workbooks/{workbook_id}/connections")
        return data.get("connections", {}).get("connection", []) if data else []

    def get_view_data(self, view_id: str, max_rows: int = 500) -> dict:
        """
        Fetch data from a Tableau view.
        The /data endpoint returns CSV but requires Accept: application/json
        (not text/csv) on Tableau Online — counterintuitive but correct.
        """
        try:
            resp = requests.get(
                f"{self.base_url}/sites/{self.site_id}/views/{view_id}/data",
                headers=self._headers,   # Accept: application/json (default)
                params={"maxRows": max_rows},
                timeout=60,
            )
            resp.raise_for_status()
            lines = resp.text.splitlines()
            return {
                "status": "success",
                "row_count": len(lines) - 1,
                "headers": lines[0] if lines else "",
                "sample": "\n".join(lines[:20]),  # up to 20 rows for the agent
                "full_data": resp.text,
            }
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def get_recent_jobs(self, workbook_id: str, job_type: str = "extractRefresh") -> list:
        data = self._get(
            "jobs",
            {
                "filter": f"type:eq:{job_type}",
                "sort": "createdAt:desc",
                "pageSize": 5,
            },
        )
        if not data:
            return []
        return [
            j for j in data.get("jobs", {}).get("job", [])
            if j.get("workbook", {}).get("id") == workbook_id
        ]


# Shared client instance
_client = TableauClient()
TABLEAU_AVAILABLE = False


def _ensure_authenticated() -> bool:
    global TABLEAU_AVAILABLE
    if _client.auth_token:
        return True
    if _client.is_configured():
        TABLEAU_AVAILABLE = _client.sign_in()
    return TABLEAU_AVAILABLE


# --- URL parsing ---


def _parse_tableau_url(url: str) -> dict:
    """
    Extract workbook/view info from a Tableau URL.

    Handles formats like:
      https://server/#/site/sitename/workbooks/12345/views
      https://server/views/WorkbookName/ViewName
    """
    result = {"server": "", "site": "", "workbook": "", "view": ""}

    server_match = re.match(r"(https?://[^/#]+)", url)
    if server_match:
        result["server"] = server_match.group(1)

    site_match = re.search(r"/#/site/([^/]+)", url)
    if site_match:
        result["site"] = site_match.group(1)

    views_match = re.search(r"/views/([^/]+)/([^/?#]+)", url)
    if views_match:
        result["workbook"] = views_match.group(1).replace("-", " ").replace("_", " ")
        result["view"] = views_match.group(2).replace("-", " ").replace("_", " ")
    else:
        wb_match = re.search(r"/workbooks/(\d+)", url)
        if wb_match:
            result["workbook_id"] = wb_match.group(1)

    return result


def _classify_issue(issue_description: str) -> str:
    desc_lower = issue_description.lower()
    if any(k in desc_lower for k in ("not loading", "blank", "error", "timeout", "failed")):
        return "not_loading"
    if any(k in desc_lower for k in ("higher", "inflated", "too high", "more than", "over")):
        return "metric_high"
    if any(k in desc_lower for k in ("lower", "missing", "less than", "under", "short")):
        return "metric_low"
    return "general"


# --- ADK Tool functions ---


def inspect_dashboard(
    tool_context: ToolContext,
    dashboard_ref: str,
    issue_description: str,
) -> dict:
    """
    Inspect a Tableau dashboard to verify the reported issue and extract
    its underlying BigQuery datasource connections.

    Args:
        dashboard_ref: Dashboard URL or workbook/view name.
        issue_description: Natural language description of the reported issue.

    Returns:
        dict with workbook metadata, datasource connections, and issue verification.
    """
    logging.info(f"Tableau: Inspecting dashboard — ref={dashboard_ref!r}, issue={issue_description!r}")

    issue_type = _classify_issue(issue_description)
    tool_context.state["issue_type"] = issue_type
    tool_context.state["issue_description"] = issue_description
    tool_context.state["dashboard_ref"] = dashboard_ref

    # Resolve workbook name
    if dashboard_ref.startswith("http"):
        parsed = _parse_tableau_url(dashboard_ref)
        workbook_name = parsed.get("workbook", "")
        view_name = parsed.get("view", "")
        tool_context.state["tableau_view_name"] = view_name
    else:
        workbook_name = dashboard_ref
        view_name = ""

    tool_context.state["tableau_workbook_name"] = workbook_name

    if _ensure_authenticated():
        # --- Real Tableau API path ---
        workbook = _client.find_workbook(workbook_name)
        if not workbook:
            return {
                "status": "error",
                "message": f"Workbook '{workbook_name}' not found on Tableau server.",
            }

        workbook_id = workbook["id"]
        tool_context.state["tableau_workbook_id"] = workbook_id

        views = _client.get_views(workbook_id)
        # Resolve the specific view referenced in the URL
        target_view = _client.find_view(views, view_name or workbook_name)
        if target_view:
            tool_context.state["tableau_view_id"] = target_view["id"]
            tool_context.state["tableau_view_name"] = target_view.get("name", view_name)
        connections = _client.get_connections(workbook_id)
        recent_jobs = _client.get_recent_jobs(workbook_id)

        last_refresh = "Unknown"
        refresh_status = "unknown"
        refresh_error = None
        if recent_jobs:
            last_job = recent_jobs[0]
            last_refresh = last_job.get("createdAt", "Unknown")
            refresh_status = last_job.get("status", "unknown")
            if refresh_status == "Failed":
                refresh_error = last_job.get("statusNotes", "No details available")

        issue_ctx = DUMMY_ISSUE_CONTEXTS.get(issue_type, DUMMY_ISSUE_CONTEXTS["general"])

        result = {
            "status": "success",
            "source": "tableau_rest_api",
            "workbook": {
                "id": workbook_id,
                "name": workbook.get("name"),
                "project": workbook.get("project", {}).get("name", ""),
                "created_at": workbook.get("createdAt", ""),
                "updated_at": workbook.get("updatedAt", ""),
                "content_url": workbook.get("contentUrl", ""),
            },
            "views": [
                {"id": v["id"], "name": v.get("name", ""), "content_url": v.get("contentUrl", "")}
                for v in views
            ],
            "datasource_connections": [
                {
                    "type": c.get("type", ""),
                    "server": c.get("serverAddress", ""),
                    "db": c.get("dbName", ""),
                    "schema": c.get("schema", ""),
                    "username": c.get("userName", ""),
                }
                for c in connections
            ],
            "last_refresh": last_refresh,
            "refresh_status": refresh_status,
            "refresh_error": refresh_error,
            "issue_verified": issue_ctx["issue_verified"],
            "verification_note": issue_ctx["verification_note"],
            "suspected_cause": issue_ctx["suspected_cause"],
        }

        tool_context.state["tableau_info"] = result
        tool_context.state["datasource_connections"] = result["datasource_connections"]
        return result

    # --- Dummy data path ---
    logging.info("Tableau: Using dummy data (API not configured)")

    key = workbook_name.lower().replace(" ", "-") if workbook_name else "revenue-overview"
    dummy = DUMMY_DASHBOARDS.get(key, list(DUMMY_DASHBOARDS.values())[0])
    issue_ctx = DUMMY_ISSUE_CONTEXTS.get(issue_type, DUMMY_ISSUE_CONTEXTS["general"])

    connections = dummy["connections"]
    tool_context.state["tableau_workbook_id"] = dummy["workbook_id"]
    tool_context.state["tableau_workbook_name"] = dummy["workbook_name"]
    tool_context.state["datasource_connections"] = connections

    result = {
        "status": "success",
        "source": "dummy_data",
        "workbook": {
            "id": dummy["workbook_id"],
            "name": dummy["workbook_name"],
            "project": dummy["project_name"],
        },
        "views": dummy["views"],
        "datasource_connections": connections,
        "last_refresh": dummy["last_refresh"],
        "refresh_status": dummy["refresh_status"],
        "refresh_error": dummy.get("refresh_error"),
        "issue_verified": issue_ctx["issue_verified"],
        "verification_note": issue_ctx["verification_note"],
        "suspected_cause": issue_ctx["suspected_cause"],
    }

    tool_context.state["tableau_info"] = result
    return result


def get_view_data_sample(
    tool_context: ToolContext,
    view_name: str = "",
) -> dict:
    """
    Fetch a sample of actual data from a Tableau view to understand
    what metrics and dimensions the dashboard is currently displaying.

    Args:
        view_name: The view/sheet name to query. Uses state if not provided.

    Returns:
        dict with sample rows and metric summary from the view.
    """
    resolved_view_name = view_name or tool_context.state.get("tableau_view_name", "")
    tableau_info = tool_context.state.get("tableau_info", {})
    logging.info(f"Tableau: Fetching view data for view='{resolved_view_name}'")

    if _ensure_authenticated():
        # Prefer the view_id already resolved by inspect_dashboard
        view_id = tool_context.state.get("tableau_view_id")
        if not view_id:
            views = tableau_info.get("views", [])
            view = _client.find_view(views, resolved_view_name or "")
            if not view:
                return {"status": "error", "message": "No matching view found."}
            view_id = view["id"]

        result = _client.get_view_data(view_id)
        tool_context.state["tableau_view_data"] = result
        return result

    # Dummy path
    workbook_name = tool_context.state.get("tableau_workbook_name", "Revenue Overview")
    key = workbook_name.lower().replace(" ", "-")
    dummy = DUMMY_DASHBOARDS.get(key, list(DUMMY_DASHBOARDS.values())[0])
    view_data_map = dummy.get("view_data", {})

    match_key = next(
        (k for k in view_data_map if resolved_view_name.lower() in k.lower()),
        next(iter(view_data_map), None),
    )

    if not match_key:
        result = {
            "status": "no_data",
            "message": "No sample data available for this view.",
        }
    else:
        vd = view_data_map[match_key]
        result = {
            "status": "success",
            "source": "dummy_data",
            "view_name": match_key,
            "primary_metric": vd.get("metric"),
            "current_value": vd.get("current_value"),
            "unit": vd.get("unit", ""),
            "period": vd.get("period", ""),
            "sample_rows": vd.get("sample_rows", []),
            "note": (
                "Current value is 50% higher than prior period, "
                "which is the reported discrepancy."
                if vd.get("current_value", 0) > 0
                else ""
            ),
        }

    tool_context.state["tableau_view_data"] = result
    return result
