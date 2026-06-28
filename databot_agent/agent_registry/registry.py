"""
Persistent registry of data-source agents.

Stores metadata (capabilities, credential env-var names, status) in a JSON file
next to this module. Credential *values* are never written here — only the env-var
key names; actual secrets live in .env.
"""

import json
import logging
import os
from datetime import datetime
from pathlib import Path

REGISTRY_PATH = Path(__file__).parent / "agents_registry.json"

_BUILTIN_AGENTS = {
    "tableau": {
        "type": "tableau",
        "name": "Tableau Inspector",
        "status": "active",
        "capabilities": ["dashboard_inspection", "view_data", "datasource_discovery"],
        "credentials_env_keys": ["TABLEAU_SERVER_URL", "TABLEAU_TOKEN_NAME", "TABLEAU_TOKEN_SECRET"],
        "module": "databot_agent.tableau_tools.tableau_api",
        "description": "Inspects Tableau dashboards, pulls live view data, and discovers datasource connections.",
        "registered_at": "builtin",
    },
    "bigquery": {
        "type": "bigquery",
        "name": "BigQuery Analyst",
        "status": "active",
        "capabilities": ["sql_query", "schema_discovery", "data_analysis", "catalog_search"],
        "credentials_env_keys": ["GOOGLE_CLOUD_PROJECT"],
        "module": "databot_agent.bigquery_utils.bigquery_tools",
        "description": "Executes read-only SQL against BigQuery and discovers table schemas.",
        "registered_at": "builtin",
    },
}


class AgentRegistry:
    """Singleton registry of all data-source agents."""

    _instance = None

    @classmethod
    def get(cls) -> "AgentRegistry":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self):
        self._data: dict = {}
        self._load()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self) -> None:
        if REGISTRY_PATH.exists():
            try:
                with open(REGISTRY_PATH) as f:
                    self._data = json.load(f)
                # Merge builtins in case new ones were added in code
                for key, val in _BUILTIN_AGENTS.items():
                    self._data.setdefault("agents", {})[key] = (
                        self._data.get("agents", {}).get(key) or val
                    )
                logging.info(f"AgentRegistry: loaded {len(self._data['agents'])} agents from {REGISTRY_PATH}")
            except Exception as e:
                logging.warning(f"AgentRegistry: could not load registry file ({e}), using defaults")
                self._data = {"agents": dict(_BUILTIN_AGENTS)}
        else:
            self._data = {"agents": dict(_BUILTIN_AGENTS)}
            self._save()

    def _save(self) -> None:
        REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(REGISTRY_PATH, "w") as f:
            json.dump(self._data, f, indent=2)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def list_agents(self) -> dict:
        return dict(self._data.get("agents", {}))

    def get_agent(self, source_type: str) -> dict | None:
        return self._data.get("agents", {}).get(source_type.lower())

    def is_registered(self, source_type: str) -> bool:
        return source_type.lower() in self._data.get("agents", {})

    def register(self, source_type: str, config: dict) -> None:
        config = dict(config)
        config["registered_at"] = datetime.now().isoformat()
        config["type"] = source_type.lower()
        config["status"] = config.get("status", "active")
        self._data.setdefault("agents", {})[source_type.lower()] = config
        self._save()
        logging.info(f"AgentRegistry: registered '{source_type}'")

    def update_status(self, source_type: str, status: str) -> None:
        agents = self._data.setdefault("agents", {})
        if source_type.lower() in agents:
            agents[source_type.lower()]["status"] = status
            agents[source_type.lower()]["status_updated_at"] = datetime.now().isoformat()
            self._save()

    def update_last_tested(self, source_type: str, success: bool) -> None:
        agents = self._data.setdefault("agents", {})
        if source_type.lower() in agents:
            agents[source_type.lower()]["last_tested"] = datetime.now().isoformat()
            agents[source_type.lower()]["last_test_passed"] = success
            self._save()
