"""Snowflake connector — supports OAuth 2.0 (preferred) and username/password."""

import logging
import os

from .base import BaseConnector


class SnowflakeConnector(BaseConnector):
    SOURCE_TYPE = "snowflake"
    DISPLAY_NAME = "Snowflake"
    DESCRIPTION = "Run SQL queries against Snowflake data warehouse."
    CAPABILITIES = ["sql_query", "schema_discovery", "warehouse_management"]

    # OAuth 2.0 preferred, basic auth (user + password) as fallback.
    AUTH_METHODS = ["oauth2", "basic"]

    # OAuth 2.0 credential fields — account + warehouse + database are also required.
    OAUTH2_FIELDS = [
        {"key": "SNOWFLAKE_CLIENT_ID",     "label": "Client ID",           "description": "OAuth 2.0 client ID from your Snowflake security integration", "required": True,  "secret": False, "example": "abc123"},
        {"key": "SNOWFLAKE_CLIENT_SECRET", "label": "Client Secret",       "description": "OAuth 2.0 client secret",                                       "required": True,  "secret": True,  "example": "xyz789"},
        {"key": "SNOWFLAKE_TOKEN_URL",     "label": "Token URL",           "description": "https://<account>.snowflakecomputing.com/oauth/token-request",  "required": True,  "secret": False, "example": "https://xy12345.snowflakecomputing.com/oauth/token-request"},
        {"key": "SNOWFLAKE_SCOPE",         "label": "Scope",               "description": "OAuth scope (e.g. 'session:role:ANALYST'). Leave blank for default.", "required": False, "secret": False, "example": "session:role:ANALYST"},
        {"key": "SNOWFLAKE_ACCOUNT",       "label": "Account identifier",  "description": "e.g. xy12345.us-east-1",                                        "required": True,  "secret": False, "example": "xy12345.us-east-1"},
        {"key": "SNOWFLAKE_WAREHOUSE",     "label": "Warehouse",           "description": "Virtual warehouse to use",                                       "required": True,  "secret": False, "example": "COMPUTE_WH"},
        {"key": "SNOWFLAKE_DATABASE",      "label": "Database",            "description": "Default database",                                               "required": True,  "secret": False, "example": "ANALYTICS"},
        {"key": "SNOWFLAKE_SCHEMA",        "label": "Schema (optional)",   "description": "Default schema",                                                 "required": False, "secret": False, "example": "PUBLIC"},
    ]

    # Basic-auth credential fields.
    BASIC_AUTH_FIELDS = [
        {"key": "SNOWFLAKE_ACCOUNT",   "label": "Account identifier", "description": "e.g. xy12345.us-east-1",             "required": True,  "secret": False, "example": "xy12345.us-east-1"},
        {"key": "SNOWFLAKE_USER",      "label": "Username",           "description": "Snowflake login username",             "required": True,  "secret": False, "example": "analyst@company.com"},
        {"key": "SNOWFLAKE_PASSWORD",  "label": "Password",           "description": "Snowflake password",                   "required": True,  "secret": True,  "example": "(your Snowflake password)"},
        {"key": "SNOWFLAKE_WAREHOUSE", "label": "Warehouse",          "description": "Virtual warehouse to use",             "required": True,  "secret": False, "example": "COMPUTE_WH"},
        {"key": "SNOWFLAKE_DATABASE",  "label": "Database",           "description": "Default database",                     "required": True,  "secret": False, "example": "ANALYTICS"},
        {"key": "SNOWFLAKE_SCHEMA",    "label": "Schema (optional)",  "description": "Default schema",                       "required": False, "secret": False, "example": "PUBLIC"},
    ]

    REQUIRED_CREDENTIAL_FIELDS = BASIC_AUTH_FIELDS

    @classmethod
    def test_connection(cls, credentials: dict) -> dict:
        try:
            import snowflake.connector
        except ImportError:
            return {
                "status": "missing_package",
                "message": "Package `snowflake-connector-python` is not installed.",
                "install_command": "pip install snowflake-connector-python",
            }
        try:
            conn = cls._build_connection(credentials)
            cur = conn.cursor()
            cur.execute("SELECT CURRENT_VERSION()")
            version = cur.fetchone()[0]
            conn.close()
            return {"status": "success", "message": f"Connected to Snowflake (version {version})"}
        except Exception as exc:
            return {"status": "error", "message": str(exc)}

    @classmethod
    def query(cls, query: str, **kwargs) -> dict:
        try:
            import snowflake.connector
        except ImportError:
            return {"status": "error", "message": "Package `snowflake-connector-python` not installed."}
        try:
            conn = cls._build_connection({})  # reads from env vars
            cur = conn.cursor()
            cur.execute(query)
            cols = [d[0] for d in cur.description] if cur.description else []
            rows = [dict(zip(cols, row)) for row in cur.fetchmany(500)]
            conn.close()
            return {"status": "success", "data": rows, "row_count": len(rows), "columns": cols}
        except Exception as exc:
            logging.error("Snowflake query error: %s", exc)
            return {"status": "error", "message": str(exc)}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @classmethod
    def _build_connection(cls, credentials: dict):
        """
        Build a snowflake.connector connection.

        Auth priority:
          1. OAuth 2.0 — if SNOWFLAKE_ACCESS_TOKEN is in credentials or env
          2. Username / password — fallback
        """
        import snowflake.connector

        def _get(key: str) -> str:
            return credentials.get(key) or os.getenv(key, "")

        account = _get("SNOWFLAKE_ACCOUNT")
        warehouse = _get("SNOWFLAKE_WAREHOUSE")
        database = _get("SNOWFLAKE_DATABASE")
        schema = _get("SNOWFLAKE_SCHEMA") or "PUBLIC"
        access_token = _get("SNOWFLAKE_ACCESS_TOKEN")

        if access_token:
            return snowflake.connector.connect(
                account=account,
                authenticator="oauth",
                token=access_token,
                warehouse=warehouse,
                database=database,
                schema=schema,
            )

        # Basic auth fallback
        return snowflake.connector.connect(
            account=account,
            user=_get("SNOWFLAKE_USER"),
            password=_get("SNOWFLAKE_PASSWORD"),
            warehouse=warehouse,
            database=database,
            schema=schema,
        )
