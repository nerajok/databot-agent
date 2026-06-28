"""Salesforce connector — supports OAuth 2.0 (preferred) and username/password."""

import logging
import os

from .base import BaseConnector


class SalesforceConnector(BaseConnector):
    SOURCE_TYPE = "salesforce"
    DISPLAY_NAME = "Salesforce"
    DESCRIPTION = "Query Salesforce objects using SOQL to retrieve CRM data (accounts, opportunities, contracts, etc.)"
    CAPABILITIES = ["soql_query", "object_discovery", "field_metadata", "report_data"]

    # Auth priority: browser-based auth code flow > client_credentials > basic auth
    AUTH_METHODS = ["oauth2_authcode", "oauth2", "basic"]

    # OAuth 2.0 Authorization Code fields — instance_url is returned by Salesforce in the
    # token response and stored automatically, so the user does not need to provide it.
    OAUTH2_AUTHCODE_FIELDS = [
        {"key": "SALESFORCE_CLIENT_ID",         "label": "Consumer Key (Client ID)",  "description": "OAuth Connected App consumer key",                                "required": True,  "secret": False, "example": "3MVG9..."},
        {"key": "SALESFORCE_CLIENT_SECRET",     "label": "Consumer Secret",           "description": "OAuth Connected App consumer secret",                             "required": True,  "secret": True,  "example": "ABC123..."},
        {"key": "SALESFORCE_AUTHORIZATION_URL", "label": "Authorization URL",         "description": "e.g. https://login.salesforce.com/services/oauth2/authorize",     "required": True,  "secret": False, "example": "https://login.salesforce.com/services/oauth2/authorize"},
        {"key": "SALESFORCE_TOKEN_URL",         "label": "Token URL",                 "description": "e.g. https://login.salesforce.com/services/oauth2/token",         "required": True,  "secret": False, "example": "https://login.salesforce.com/services/oauth2/token"},
        {"key": "SALESFORCE_SCOPE",             "label": "Scope",                     "description": "OAuth scopes (e.g. api refresh_token offline_access)",            "required": False, "secret": False, "example": "api refresh_token offline_access"},
    ]

    # OAuth 2.0 Client Credentials fields — INSTANCE_URL required for this grant type.
    OAUTH2_FIELDS = [
        {"key": "SALESFORCE_CLIENT_ID",     "label": "Consumer Key (Client ID)",    "description": "OAuth Connected App consumer key",  "required": True,  "secret": False, "example": "3MVG9..."},
        {"key": "SALESFORCE_CLIENT_SECRET", "label": "Consumer Secret",              "description": "OAuth Connected App consumer secret","required": True,  "secret": True,  "example": "ABC123..."},
        {"key": "SALESFORCE_TOKEN_URL",     "label": "Token URL",                   "description": "e.g. https://login.salesforce.com/services/oauth2/token", "required": True, "secret": False, "example": "https://login.salesforce.com/services/oauth2/token"},
        {"key": "SALESFORCE_INSTANCE_URL",  "label": "Instance URL",                "description": "Your Salesforce org URL",            "required": True,  "secret": False, "example": "https://yourcompany.my.salesforce.com"},
        {"key": "SALESFORCE_SCOPE",         "label": "Scope",                       "description": "OAuth scope (leave blank for default)", "required": False, "secret": False, "example": "api"},
    ]

    # Basic-auth credential fields (legacy / fallback).
    BASIC_AUTH_FIELDS = [
        {"key": "SALESFORCE_INSTANCE_URL",    "label": "Instance URL",    "description": "Your Salesforce org URL",                     "required": True, "secret": False, "example": "https://yourcompany.my.salesforce.com"},
        {"key": "SALESFORCE_USERNAME",        "label": "Username",        "description": "Salesforce login username / email",            "required": True, "secret": False, "example": "admin@yourcompany.com"},
        {"key": "SALESFORCE_PASSWORD",        "label": "Password",        "description": "Salesforce password",                         "required": True, "secret": True,  "example": "(your Salesforce password)"},
        {"key": "SALESFORCE_SECURITY_TOKEN",  "label": "Security Token",  "description": "From Settings > Reset My Security Token",     "required": True, "secret": True,  "example": "xxxxxxxxxxxxxxxx"},
    ]

    # Keep REQUIRED_CREDENTIAL_FIELDS pointing at basic-auth for legacy/custom fallback.
    REQUIRED_CREDENTIAL_FIELDS = BASIC_AUTH_FIELDS

    @classmethod
    def test_connection(cls, credentials: dict) -> dict:
        try:
            from simple_salesforce import Salesforce
        except ImportError:
            return {
                "status": "missing_package",
                "message": "Package `simple_salesforce` is not installed.",
                "install_command": "pip install simple_salesforce",
            }
        try:
            sf = cls._build_client(credentials)
            result = sf.query("SELECT Id, Name FROM Organization LIMIT 1")
            org_name = result["records"][0].get("Name", "Unknown") if result["records"] else "Unknown"
            return {"status": "success", "message": f"Connected to Salesforce org: {org_name}"}
        except Exception as exc:
            return {"status": "error", "message": str(exc)}

    @classmethod
    def query(cls, query: str, **kwargs) -> dict:
        try:
            from simple_salesforce import Salesforce
        except ImportError:
            return {"status": "error", "message": "Package `simple_salesforce` not installed."}
        try:
            sf = cls._build_client({})  # reads from env vars
            result = sf.query_all(query)
            records = result.get("records", [])
            clean = [{k: v for k, v in r.items() if k != "attributes"} for r in records]
            return {"status": "success", "data": clean, "row_count": len(clean), "total_size": result.get("totalSize", len(clean))}
        except Exception as exc:
            logging.error("Salesforce query error: %s", exc)
            return {"status": "error", "message": str(exc)}

    @classmethod
    def list_objects(cls) -> dict:
        """List all queryable Salesforce objects."""
        try:
            from simple_salesforce import Salesforce
            sf = cls._build_client({})
            desc = sf.describe()
            queryable = [s["name"] for s in desc["sobjects"] if s.get("queryable")]
            return {"status": "success", "objects": queryable[:50]}
        except Exception as exc:
            return {"status": "error", "message": str(exc)}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @classmethod
    def _build_client(cls, credentials: dict):
        """
        Build a simple_salesforce.Salesforce client.

        Auth priority:
          1. OAuth 2.0 — if SALESFORCE_ACCESS_TOKEN is in credentials or env
          2. Username / password — fallback
        """
        from simple_salesforce import Salesforce

        instance_url = (
            credentials.get("SALESFORCE_INSTANCE_URL")
            or os.getenv("SALESFORCE_INSTANCE_URL", "")
        )
        access_token = (
            credentials.get("SALESFORCE_ACCESS_TOKEN")
            or os.getenv("SALESFORCE_ACCESS_TOKEN", "")
        )

        if access_token and instance_url:
            return Salesforce(instance_url=instance_url, session_id=access_token)

        # Basic auth fallback
        return Salesforce(
            instance_url=instance_url,
            username=credentials.get("SALESFORCE_USERNAME") or os.getenv("SALESFORCE_USERNAME", ""),
            password=credentials.get("SALESFORCE_PASSWORD") or os.getenv("SALESFORCE_PASSWORD", ""),
            security_token=credentials.get("SALESFORCE_SECURITY_TOKEN") or os.getenv("SALESFORCE_SECURITY_TOKEN", ""),
        )
