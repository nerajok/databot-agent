"""REST API connector — supports OAuth 2.0 (preferred) and basic/bearer/apikey auth."""

import base64
import logging
import os

import requests

from .base import BaseConnector


class RestApiConnector(BaseConnector):
    SOURCE_TYPE = "rest_api"
    DISPLAY_NAME = "REST API"
    DESCRIPTION = "Fetch data from any REST API endpoint (JSON responses)."
    CAPABILITIES = ["http_get", "http_post", "json_data_retrieval"]

    # OAuth 2.0 preferred (factory exchanges credentials → stores bearer token),
    # basic auth (username:password or pre-issued token) as fallback.
    AUTH_METHODS = ["oauth2", "basic"]

    # OAuth 2.0 fields — after exchange, the token is used as a Bearer token.
    OAUTH2_FIELDS = [
        {"key": "REST_API_CLIENT_ID",     "label": "Client ID",     "description": "OAuth 2.0 client ID",                        "required": True,  "secret": False, "example": "my-client-id"},
        {"key": "REST_API_CLIENT_SECRET", "label": "Client Secret", "description": "OAuth 2.0 client secret",                    "required": True,  "secret": True,  "example": "my-client-secret"},
        {"key": "REST_API_TOKEN_URL",     "label": "Token URL",     "description": "OAuth 2.0 token endpoint URL",               "required": True,  "secret": False, "example": "https://auth.example.com/oauth/token"},
        {"key": "REST_API_SCOPE",         "label": "Scope",         "description": "OAuth scope(s), space-separated. Optional.", "required": False, "secret": False, "example": "read:data"},
        {"key": "REST_API_BASE_URL",      "label": "Base URL",      "description": "API base URL",                               "required": True,  "secret": False, "example": "https://api.example.com/v1"},
    ]

    # Basic-auth / pre-issued token fields.
    BASIC_AUTH_FIELDS = [
        {"key": "REST_API_BASE_URL",    "label": "Base URL",                          "description": "API base URL",                                              "required": True,  "secret": False, "example": "https://api.example.com/v1"},
        {"key": "REST_API_AUTH_TYPE",   "label": "Auth type",                         "description": "bearer | apikey | basic",                                   "required": True,  "secret": False, "example": "bearer"},
        {"key": "REST_API_AUTH_VALUE",  "label": "Auth value (token / password)",     "description": "Bearer token, API key, or base64 user:pass",               "required": True,  "secret": True,  "example": "your-token-here"},
        {"key": "REST_API_AUTH_HEADER", "label": "Auth header name (apikey only)",    "description": "Header name when auth type is apikey, e.g. X-Api-Key",     "required": False, "secret": False, "example": "X-Api-Key"},
    ]

    REQUIRED_CREDENTIAL_FIELDS = BASIC_AUTH_FIELDS

    @classmethod
    def test_connection(cls, credentials: dict) -> dict:
        base_url = (
            credentials.get("REST_API_BASE_URL") or os.getenv("REST_API_BASE_URL", "")
        ).rstrip("/")
        if not base_url:
            return {"status": "error", "message": "REST_API_BASE_URL is required."}

        headers = cls._build_headers(credentials)
        try:
            resp = requests.get(base_url, headers=headers, timeout=10)
            if resp.status_code < 500:
                return {
                    "status": "success",
                    "message": f"Reachable — HTTP {resp.status_code} from {base_url}",
                    "http_status": resp.status_code,
                }
            return {"status": "error", "message": f"HTTP {resp.status_code} from {base_url}"}
        except Exception as exc:
            return {"status": "error", "message": str(exc)}

    @classmethod
    def query(cls, query: str, **kwargs) -> dict:
        """
        `query` is a path relative to REST_API_BASE_URL, e.g. "/reports/123".
        Supports method="GET"|"POST" and body=dict for POST requests.
        """
        base_url = (os.getenv("REST_API_BASE_URL") or "").rstrip("/")
        path = query if query.startswith("/") else f"/{query}"
        full_url = base_url + path
        headers = cls._build_headers({})  # reads from env vars

        method = kwargs.get("method", "GET").upper()
        body = kwargs.get("body")

        try:
            if method == "POST":
                resp = requests.post(full_url, headers=headers, json=body, timeout=30)
            else:
                resp = requests.get(full_url, headers=headers, timeout=30)

            resp.raise_for_status()
            data = resp.json() if resp.content else {}
            row_count = len(data) if isinstance(data, list) else 1
            return {"status": "success", "data": data, "row_count": row_count, "url": full_url}
        except Exception as exc:
            logging.error("REST API query error: %s", exc)
            return {"status": "error", "message": str(exc), "url": full_url}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @classmethod
    def _build_headers(cls, credentials: dict) -> dict:
        """
        Build request headers.

        Auth priority:
          1. OAuth 2.0 — if REST_API_ACCESS_TOKEN is in credentials or env
             (factory writes this after performing the token exchange)
          2. Explicit auth type (bearer / apikey / basic) from env vars
        """
        # 1. OAuth 2.0 token (written by factory after exchange)
        access_token = (
            credentials.get("REST_API_ACCESS_TOKEN")
            or os.getenv("REST_API_ACCESS_TOKEN", "")
        )
        if access_token:
            return {"Authorization": f"Bearer {access_token}"}

        # 2. Explicit auth type from env or credentials dict
        def _get(key: str) -> str:
            return credentials.get(key) or os.getenv(key, "")

        auth_type = _get("REST_API_AUTH_TYPE").lower()
        auth_value = _get("REST_API_AUTH_VALUE")

        if auth_type == "bearer":
            return {"Authorization": f"Bearer {auth_value}"}
        if auth_type == "apikey":
            header_name = _get("REST_API_AUTH_HEADER") or "X-Api-Key"
            return {header_name: auth_value}
        if auth_type == "basic":
            encoded = base64.b64encode(auth_value.encode()).decode()
            return {"Authorization": f"Basic {encoded}"}

        return {}
