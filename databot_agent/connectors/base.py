"""
Base connector interface. Every data-source connector must implement this.
"""

from abc import ABC, abstractmethod


class BaseConnector(ABC):
    """Abstract base for all data-source connectors."""

    # --- Required: subclasses must define these ---
    SOURCE_TYPE: str = ""
    DISPLAY_NAME: str = ""
    DESCRIPTION: str = ""
    CAPABILITIES: list[str] = []

    # Credential fields used when no structured auth method is declared.
    # e.g. [{"key": "SALESFORCE_INSTANCE_URL", "label": "Instance URL", "example": "https://yourcompany.my.salesforce.com"}]
    REQUIRED_CREDENTIAL_FIELDS: list[dict] = []

    # --- Optional: declare supported auth methods in priority order ---
    # auth_manager checks this to decide which credential form to show.
    # ["oauth2", "basic"]  → OAuth 2.0 preferred, basic-auth fallback
    # ["basic"]            → basic auth only
    # []                   → falls back to REQUIRED_CREDENTIAL_FIELDS (legacy)
    AUTH_METHODS: list[str] = []

    # Override to customise the OAuth 2.0 credential fields shown to the user.
    # Keys must use {ST} as a placeholder for SOURCE_TYPE.upper().
    # If None, auth_manager uses its built-in defaults.
    OAUTH2_FIELDS: list[dict] | None = None

    # Override to customise the basic-auth credential fields.
    # If None, auth_manager uses username + password defaults.
    BASIC_AUTH_FIELDS: list[dict] | None = None

    @classmethod
    @abstractmethod
    def test_connection(cls, credentials: dict) -> dict:
        """
        Test a connection with the supplied credential dict.
        Returns {"status": "success"|"error", "message": str}.
        """

    @classmethod
    @abstractmethod
    def query(cls, query: str, **kwargs) -> dict:
        """
        Run a query against the source using env-var credentials.
        Returns {"status": "success"|"error", "data": ..., "row_count": int}.
        """

    @classmethod
    def credential_prompt(cls) -> str:
        """Return a human-readable list of required credentials."""
        lines = [f"To connect to **{cls.DISPLAY_NAME}**, I need the following credentials:\n"]
        for i, field in enumerate(cls.REQUIRED_CREDENTIAL_FIELDS, 1):
            example = f" (e.g. `{field['example']}`)" if field.get("example") else ""
            lines.append(f"  {i}. **{field['label']}**{example}")
        lines.append(
            "\nPlease reply with these values. They will be saved to your `.env` file "
            "and **never** stored in the agent registry JSON."
        )
        return "\n".join(lines)
