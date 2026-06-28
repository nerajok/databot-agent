"""
Auth Manager — OAuth 2.0 and basic-auth credential routing for the agent factory.

Priority for every new connector-backed agent:
  1. OAuth 2.0 Authorization Code flow   — if connector declares AUTH_METHODS with "oauth2_authcode"
  2. OAuth 2.0 Client Credentials grant  — if connector declares AUTH_METHODS with "oauth2"
  3. Username / Password (basic auth)    — if connector declares AUTH_METHODS with "basic"
  4. Custom fields (REQUIRED_CREDENTIAL_FIELDS) — fallback for connectors without AUTH_METHODS

OAuth 2.0 Authorization Code flow:
  Opens browser → user logs in and authorizes → redirect to localhost:8765/callback with ?code=
  → exchange code for access_token + refresh_token
  → tokens written to .env as {SOURCE_TYPE}_ACCESS_TOKEN / {SOURCE_TYPE}_REFRESH_TOKEN
  → instance_url (if returned, e.g. Salesforce) written as {SOURCE_TYPE}_INSTANCE_URL

OAuth 2.0 Client Credentials flow:
  POST {TOKEN_URL} with grant_type=client_credentials, client_id, client_secret, scope
  → access_token written to .env as {SOURCE_TYPE}_ACCESS_TOKEN

Public API used by factory_tools.py:
  get_preferred_auth_method(connector_cls) -> str
  build_credential_fields(connector_cls, auth_method) -> list[dict]
  build_credential_prompt_text(connector_cls, auth_method, fields) -> str
  perform_oauth2_exchange(credentials, source_type) -> dict
  perform_oauth2_authcode_flow(credentials, source_type, redirect_port=8765) -> dict
  perform_refresh_token_exchange(credentials, source_type) -> dict
  store_oauth_token(source_type, token_data, env_path) -> str   (returns env var key)
  refresh_oauth_token_for(source_type, env_path) -> dict
"""

import logging
import os
import threading
from pathlib import Path

import requests
from dotenv import set_key

logger = logging.getLogger(__name__)

AUTH_METHOD_OAUTH2_AUTHCODE = "oauth2_authcode"
AUTH_METHOD_OAUTH2 = "oauth2"
AUTH_METHOD_BASIC = "basic"
AUTH_METHOD_CUSTOM = "custom"

# ---------------------------------------------------------------------------
# Default credential field templates
# {SOURCE_TYPE} is replaced with the connector's actual SOURCE_TYPE.upper()
# ---------------------------------------------------------------------------

_DEFAULT_OAUTH2_AUTHCODE_FIELDS: list[dict] = [
    {
        "key": "{ST}_CLIENT_ID",
        "label": "Client ID",
        "description": "OAuth 2.0 application client ID (Consumer Key)",
        "required": True,
        "secret": False,
        "example": "your-client-id",
    },
    {
        "key": "{ST}_CLIENT_SECRET",
        "label": "Client Secret",
        "description": "OAuth 2.0 application client secret (Consumer Secret)",
        "required": True,
        "secret": True,
        "example": "your-client-secret",
    },
    {
        "key": "{ST}_AUTHORIZATION_URL",
        "label": "Authorization URL",
        "description": "OAuth 2.0 authorization endpoint — the browser redirect target",
        "required": True,
        "secret": False,
        "example": "https://login.example.com/oauth2/authorize",
    },
    {
        "key": "{ST}_TOKEN_URL",
        "label": "Token URL",
        "description": "OAuth 2.0 token endpoint for authorization code exchange",
        "required": True,
        "secret": False,
        "example": "https://login.example.com/oauth2/token",
    },
    {
        "key": "{ST}_SCOPE",
        "label": "Scope",
        "description": "Space-separated OAuth 2.0 scopes. Leave blank if not required.",
        "required": False,
        "secret": False,
        "example": "read:data api offline_access",
    },
]

_DEFAULT_OAUTH2_FIELDS: list[dict] = [
    {
        "key": "{ST}_CLIENT_ID",
        "label": "Client ID",
        "description": "OAuth 2.0 application client ID (Consumer Key)",
        "required": True,
        "secret": False,
        "example": "3MVG9...",
    },
    {
        "key": "{ST}_CLIENT_SECRET",
        "label": "Client Secret",
        "description": "OAuth 2.0 application client secret (Consumer Secret)",
        "required": True,
        "secret": True,
        "example": "ABC123...",
    },
    {
        "key": "{ST}_TOKEN_URL",
        "label": "Token URL",
        "description": "OAuth 2.0 token endpoint for the client_credentials grant",
        "required": True,
        "secret": False,
        "example": "https://login.example.com/oauth2/token",
    },
    {
        "key": "{ST}_SCOPE",
        "label": "Scope",
        "description": "Space-separated OAuth 2.0 scope(s). Leave blank if not required.",
        "required": False,
        "secret": False,
        "example": "read:data api",
    },
]

_DEFAULT_BASIC_AUTH_FIELDS: list[dict] = [
    {
        "key": "{ST}_USERNAME",
        "label": "Username / Email",
        "description": "Login username or email address",
        "required": True,
        "secret": False,
        "example": "admin@company.com",
    },
    {
        "key": "{ST}_PASSWORD",
        "label": "Password",
        "description": "Login password or API key",
        "required": True,
        "secret": True,
        "example": "(your password)",
    },
]


# ---------------------------------------------------------------------------
# Core helpers
# ---------------------------------------------------------------------------

def get_preferred_auth_method(connector_cls) -> str:
    """
    Return the highest-priority supported auth method for a connector.

    Checks connector_cls.AUTH_METHODS (list) in order:
      ["oauth2_authcode", ...]  → returns "oauth2_authcode"
      ["oauth2", ...]           → returns "oauth2"
      ["basic"]                 → returns "basic"
      []  / not defined         → returns "custom" (uses REQUIRED_CREDENTIAL_FIELDS)
    """
    auth_methods: list[str] = getattr(connector_cls, "AUTH_METHODS", []) or []
    if AUTH_METHOD_OAUTH2_AUTHCODE in auth_methods:
        return AUTH_METHOD_OAUTH2_AUTHCODE
    if AUTH_METHOD_OAUTH2 in auth_methods:
        return AUTH_METHOD_OAUTH2
    if AUTH_METHOD_BASIC in auth_methods:
        return AUTH_METHOD_BASIC
    return AUTH_METHOD_CUSTOM


def build_credential_fields(connector_cls, auth_method: str) -> list[dict]:
    """
    Return the credential field definitions for the given auth method.

    For oauth2_authcode/oauth2/basic, checks for a connector-level override first:
      connector_cls.OAUTH2_AUTHCODE_FIELDS / connector_cls.OAUTH2_FIELDS / connector_cls.BASIC_AUTH_FIELDS
    Falls back to the default templates with SOURCE_TYPE substituted.
    """
    st = getattr(connector_cls, "SOURCE_TYPE", "source").upper()

    if auth_method == AUTH_METHOD_OAUTH2_AUTHCODE:
        raw = getattr(connector_cls, "OAUTH2_AUTHCODE_FIELDS", None) or _DEFAULT_OAUTH2_AUTHCODE_FIELDS
        return [{**f, "key": f["key"].replace("{ST}", st)} for f in raw]

    if auth_method == AUTH_METHOD_OAUTH2:
        raw = getattr(connector_cls, "OAUTH2_FIELDS", None) or _DEFAULT_OAUTH2_FIELDS
        return [{**f, "key": f["key"].replace("{ST}", st)} for f in raw]

    if auth_method == AUTH_METHOD_BASIC:
        raw = getattr(connector_cls, "BASIC_AUTH_FIELDS", None) or _DEFAULT_BASIC_AUTH_FIELDS
        # Also include any connector-specific fields that aren't already covered
        extra = [
            f for f in getattr(connector_cls, "REQUIRED_CREDENTIAL_FIELDS", [])
            if not any(kw in f["key"].upper() for kw in ("USERNAME", "PASSWORD", "USER", "PASS"))
        ]
        return [{**f, "key": f["key"].replace("{ST}", st)} for f in raw] + extra

    # Custom: use REQUIRED_CREDENTIAL_FIELDS as-is
    return getattr(connector_cls, "REQUIRED_CREDENTIAL_FIELDS", [])


def build_credential_prompt_text(
    connector_cls, auth_method: str, fields: list[dict]
) -> str:
    """Return a human-readable credential prompt for the given auth method."""
    display_name = getattr(connector_cls, "DISPLAY_NAME", connector_cls.__name__)

    if auth_method == AUTH_METHOD_OAUTH2_AUTHCODE:
        header = f"OAuth 2.0 Authorization Code — {display_name}"
        method_note = (
            "Using the authorization_code grant (browser login). "
            "Once you provide the credentials below, a browser window will open for you to "
            "log in and authorize access. The agent captures the redirect automatically — "
            "you do NOT need to paste any token."
        )
    elif auth_method == AUTH_METHOD_OAUTH2:
        header = f"OAuth 2.0 Authentication — {display_name}"
        method_note = (
            "Using the client_credentials grant (recommended for server-to-server). "
            "I will exchange your credentials for an access token automatically — "
            "you do NOT need to paste a token."
        )
    elif auth_method == AUTH_METHOD_BASIC:
        header = f"Username / Password Authentication — {display_name}"
        method_note = (
            "OAuth 2.0 is not supported by this connector. "
            "Using username and password instead."
        )
    else:
        header = f"API Credentials — {display_name}"
        method_note = ""

    lines = [header, "─" * len(header)]
    if method_note:
        lines += [method_note, ""]

    for i, field in enumerate(fields, 1):
        secret_hint = "  [masked]" if field.get("secret") else ""
        optional_hint = "  [optional]" if not field.get("required", True) else ""
        example = f"  e.g. {field.get('example', '')}" if field.get("example") else ""
        lines.append(f"  {i}. {field['label']}{optional_hint}{secret_hint}")
        lines.append(f"     Env var : {field['key']}")
        lines.append(f"     {field.get('description', '')}{example}")
        lines.append("")

    lines.append("Please provide the values above.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# OAuth 2.0 token exchange
# ---------------------------------------------------------------------------

def perform_oauth2_exchange(credentials: dict, source_type: str) -> dict:
    """
    Execute an OAuth 2.0 client_credentials token exchange.

    Reads client_id, client_secret, token_url, scope from the credentials dict
    (falls back to env vars for any key not in the dict).

    Returns:
        {"status": "success", "access_token": ..., "token_type": ..., "expires_in": ...}
        {"status": "error", "error": str}
    """
    st = source_type.upper()

    def _get(key_suffix: str) -> str:
        env_key = f"{st}_{key_suffix}"
        return credentials.get(env_key) or os.getenv(env_key, "")

    client_id = _get("CLIENT_ID")
    client_secret = _get("CLIENT_SECRET")
    token_url = _get("TOKEN_URL")
    scope = _get("SCOPE")

    if not (client_id and client_secret and token_url):
        missing = [
            k for k, v in {
                f"{st}_CLIENT_ID": client_id,
                f"{st}_CLIENT_SECRET": client_secret,
                f"{st}_TOKEN_URL": token_url,
            }.items()
            if not v
        ]
        return {
            "status": "error",
            "error": f"Missing required OAuth 2.0 fields: {missing}",
        }

    payload: dict = {
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
    }
    if scope:
        payload["scope"] = scope

    try:
        logger.info("AuthManager: exchanging OAuth2 token for '%s' at %s", source_type, token_url)
        resp = requests.post(token_url, data=payload, timeout=15)
        resp.raise_for_status()
        token_data: dict = resp.json()
    except requests.exceptions.HTTPError as exc:
        body = exc.response.text[:400] if exc.response is not None else ""
        return {
            "status": "error",
            "error": f"HTTP {exc.response.status_code if exc.response is not None else '?'}: {body}",
        }
    except requests.exceptions.RequestException as exc:
        return {"status": "error", "error": f"Request failed: {exc}"}
    except Exception as exc:
        return {"status": "error", "error": f"Unexpected error: {exc}"}

    access_token = token_data.get("access_token")
    if not access_token:
        return {
            "status": "error",
            "error": (
                "Token endpoint responded 200 but no access_token in body. "
                f"Keys returned: {list(token_data.keys())}"
            ),
        }

    return {
        "status": "success",
        "access_token": access_token,
        "token_type": token_data.get("token_type", "Bearer"),
        "expires_in": token_data.get("expires_in"),
        "scope": token_data.get("scope", scope),
    }


def perform_oauth2_authcode_flow(
    credentials: dict, source_type: str, redirect_port: int = 8765
) -> dict:
    """
    Execute an OAuth 2.0 Authorization Code flow.

    Opens the browser for the user to log in and authorize, starts a local
    HTTP server on redirect_port to capture the callback, then exchanges the
    authorization code for access and refresh tokens.

    Returns:
        {"status": "success", "access_token": ..., "refresh_token": ..., "instance_url": ...}
        {"status": "error", "error": str}
    """
    import secrets
    import webbrowser
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from urllib.parse import parse_qs, urlencode, urlparse

    st = source_type.upper()

    def _get(key_suffix: str) -> str:
        env_key = f"{st}_{key_suffix}"
        return credentials.get(env_key) or os.getenv(env_key, "")

    client_id = _get("CLIENT_ID")
    client_secret = _get("CLIENT_SECRET")
    auth_url = _get("AUTHORIZATION_URL") or _get("AUTH_URL")
    token_url = _get("TOKEN_URL")
    scope = _get("SCOPE")
    redirect_uri = f"http://localhost:{redirect_port}/callback"
    state = secrets.token_urlsafe(16)

    if not (client_id and auth_url and token_url):
        missing = [
            k for k, v in {
                f"{st}_CLIENT_ID": client_id,
                f"{st}_AUTHORIZATION_URL": auth_url,
                f"{st}_TOKEN_URL": token_url,
            }.items() if not v
        ]
        return {"status": "error", "error": f"Missing required OAuth2 auth code fields: {missing}"}

    params: dict = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "state": state,
    }
    if scope:
        params["scope"] = scope

    full_auth_url = f"{auth_url}?{urlencode(params)}"
    callback_result: dict = {"code": None, "state": None, "error": None}

    class _CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            if "code" in query:
                callback_result["code"] = query["code"][0]
                callback_result["state"] = query.get("state", [None])[0]
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(
                    b"<html><body><h1>Authorization successful!</h1>"
                    b"<p>You can close this tab and return to the terminal.</p></body></html>"
                )
            elif "error" in query:
                callback_result["error"] = query["error"][0]
                self.send_response(400)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(
                    b"<html><body><h1>Authorization failed.</h1>"
                    b"<p>Check the error in your terminal and try again.</p></body></html>"
                )
            else:
                self.send_response(404)
                self.end_headers()
            threading.Thread(target=self.server.shutdown, daemon=True).start()

        def log_message(self, format, *args):  # suppress access logs
            pass

    try:
        httpd = HTTPServer(("localhost", redirect_port), _CallbackHandler)
    except OSError as exc:
        return {"status": "error", "error": f"Could not start local callback server on port {redirect_port}: {exc}"}

    server_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    server_thread.start()

    logger.info("AuthManager: opening browser for %s OAuth2 authorization...", source_type)
    print(f"\nOpening browser for {source_type.capitalize()} authorization...")
    print(f"If the browser does not open automatically, visit:\n  {full_auth_url}\n")
    webbrowser.open(full_auth_url)

    server_thread.join(timeout=300)
    if server_thread.is_alive():
        httpd.shutdown()
        return {"status": "error", "error": "Authorization timed out after 5 minutes — no callback received."}

    if callback_result["error"]:
        return {"status": "error", "error": f"Authorization denied: {callback_result['error']}"}

    if not callback_result["code"]:
        return {"status": "error", "error": "No authorization code received in callback."}

    if callback_result["state"] != state:
        return {"status": "error", "error": "State mismatch in callback — possible CSRF. Please retry."}

    # Exchange authorization code for tokens
    payload: dict = {
        "grant_type": "authorization_code",
        "code": callback_result["code"],
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "client_secret": client_secret,
    }

    try:
        logger.info("AuthManager: exchanging auth code for tokens (%s) at %s", source_type, token_url)
        resp = requests.post(token_url, data=payload, timeout=15)
        resp.raise_for_status()
        token_data: dict = resp.json()
    except requests.exceptions.HTTPError as exc:
        body = exc.response.text[:400] if exc.response is not None else ""
        return {
            "status": "error",
            "error": f"Token exchange HTTP {exc.response.status_code if exc.response is not None else '?'}: {body}",
        }
    except requests.exceptions.RequestException as exc:
        return {"status": "error", "error": f"Request failed: {exc}"}

    access_token = token_data.get("access_token")
    if not access_token:
        return {
            "status": "error",
            "error": (
                "Token endpoint responded 200 but no access_token in body. "
                f"Keys returned: {list(token_data.keys())}"
            ),
        }

    return {
        "status": "success",
        "access_token": access_token,
        "refresh_token": token_data.get("refresh_token"),
        "token_type": token_data.get("token_type", "Bearer"),
        "expires_in": token_data.get("expires_in"),
        "scope": token_data.get("scope", scope),
        "instance_url": token_data.get("instance_url"),
    }


def perform_refresh_token_exchange(credentials: dict, source_type: str) -> dict:
    """
    Exchange a stored refresh_token for a new access_token.

    Returns the same shape as perform_oauth2_exchange().
    """
    st = source_type.upper()

    def _get(key_suffix: str) -> str:
        env_key = f"{st}_{key_suffix}"
        return credentials.get(env_key) or os.getenv(env_key, "")

    client_id = _get("CLIENT_ID")
    client_secret = _get("CLIENT_SECRET")
    token_url = _get("TOKEN_URL")
    refresh_token = _get("REFRESH_TOKEN")

    if not (client_id and token_url and refresh_token):
        missing = [
            k for k, v in {
                f"{st}_CLIENT_ID": client_id,
                f"{st}_TOKEN_URL": token_url,
                f"{st}_REFRESH_TOKEN": refresh_token,
            }.items() if not v
        ]
        return {"status": "error", "error": f"Missing required refresh token fields: {missing}"}

    payload: dict = {
        "grant_type": "refresh_token",
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
    }

    try:
        resp = requests.post(token_url, data=payload, timeout=15)
        resp.raise_for_status()
        token_data: dict = resp.json()
    except requests.exceptions.HTTPError as exc:
        body = exc.response.text[:400] if exc.response is not None else ""
        return {
            "status": "error",
            "error": f"HTTP {exc.response.status_code if exc.response is not None else '?'}: {body}",
        }
    except requests.exceptions.RequestException as exc:
        return {"status": "error", "error": f"Request failed: {exc}"}

    access_token = token_data.get("access_token")
    if not access_token:
        return {
            "status": "error",
            "error": (
                "Refresh token endpoint responded 200 but no access_token. "
                f"Keys returned: {list(token_data.keys())}"
            ),
        }

    return {
        "status": "success",
        "access_token": access_token,
        # Some providers rotate refresh tokens; keep the old one if not rotated
        "refresh_token": token_data.get("refresh_token", refresh_token),
        "token_type": token_data.get("token_type", "Bearer"),
        "expires_in": token_data.get("expires_in"),
        "scope": token_data.get("scope"),
        "instance_url": token_data.get("instance_url"),
    }


def store_oauth_token(source_type: str, token_data: dict, env_path: Path) -> str:
    """
    Write the access token (and optional refresh token / instance_url) to .env.
    Also writes to os.environ so the current process can use them immediately.

    Returns the access token env var key (e.g. "SALESFORCE_ACCESS_TOKEN").
    """
    st = source_type.upper()
    token_key = f"{st}_ACCESS_TOKEN"
    if not env_path.exists():
        env_path.touch()
    set_key(str(env_path), token_key, token_data["access_token"])
    os.environ[token_key] = token_data["access_token"]

    if token_data.get("refresh_token"):
        refresh_key = f"{st}_REFRESH_TOKEN"
        set_key(str(env_path), refresh_key, token_data["refresh_token"])
        os.environ[refresh_key] = token_data["refresh_token"]
        logger.info("AuthManager: stored %s in .env", refresh_key)

    if token_data.get("instance_url"):
        instance_key = f"{st}_INSTANCE_URL"
        set_key(str(env_path), instance_key, token_data["instance_url"])
        os.environ[instance_key] = token_data["instance_url"]
        logger.info("AuthManager: stored %s in .env", instance_key)

    logger.info("AuthManager: stored %s in .env", token_key)
    return token_key


def refresh_oauth_token_for(source_type: str, env_path: Path) -> dict:
    """
    Re-acquire a fresh access token for a registered OAuth2 source.

    Strategy (in order):
      1. refresh_token grant — if {ST}_REFRESH_TOKEN is in the environment
      2. client_credentials grant — fallback for sources without a refresh token

    Returns the same shape as perform_oauth2_exchange() plus "token_key".
    """
    st = source_type.upper()
    refresh_token = os.getenv(f"{st}_REFRESH_TOKEN", "")

    if refresh_token:
        credentials = {
            f"{st}_CLIENT_ID": os.getenv(f"{st}_CLIENT_ID", ""),
            f"{st}_CLIENT_SECRET": os.getenv(f"{st}_CLIENT_SECRET", ""),
            f"{st}_TOKEN_URL": os.getenv(f"{st}_TOKEN_URL", ""),
            f"{st}_REFRESH_TOKEN": refresh_token,
        }
        result = perform_refresh_token_exchange(credentials, source_type)
    else:
        credentials = {
            f"{st}_CLIENT_ID": os.getenv(f"{st}_CLIENT_ID", ""),
            f"{st}_CLIENT_SECRET": os.getenv(f"{st}_CLIENT_SECRET", ""),
            f"{st}_TOKEN_URL": os.getenv(f"{st}_TOKEN_URL", ""),
            f"{st}_SCOPE": os.getenv(f"{st}_SCOPE", ""),
        }
        result = perform_oauth2_exchange(credentials, source_type)

    if result["status"] != "success":
        return result

    token_key = store_oauth_token(source_type, result, env_path)
    result["token_key"] = token_key
    return result
