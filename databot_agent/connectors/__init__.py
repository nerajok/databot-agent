"""
Connector registry — auto-discovers all BaseConnector subclasses in this directory.

To add a new connector: create connectors/<source_type>.py with a class extending
BaseConnector. It will be registered automatically on next import.
"""

import importlib
import logging
from pathlib import Path

from .base import BaseConnector

CONNECTOR_REGISTRY: dict[str, type] = {}

for _path in sorted(Path(__file__).parent.glob("*.py")):
    if _path.stem in ("__init__", "base"):
        continue
    try:
        _mod = importlib.import_module(f"databot_agent.connectors.{_path.stem}")
        for _name in dir(_mod):
            _obj = getattr(_mod, _name)
            if (
                isinstance(_obj, type)
                and issubclass(_obj, BaseConnector)
                and _obj is not BaseConnector
                and _obj.SOURCE_TYPE
            ):
                CONNECTOR_REGISTRY[_obj.SOURCE_TYPE] = _obj
    except Exception as _e:
        logging.debug(f"connectors: skipped {_path.stem}: {_e}")


def get_connector(source_type: str):
    """Return the connector class for a source type, or None if unknown."""
    return CONNECTOR_REGISTRY.get(source_type.lower())


def list_supported_types() -> list[str]:
    """Return all source types the factory knows how to create."""
    return list(CONNECTOR_REGISTRY.keys())


def reload_registry() -> None:
    """Re-scan connectors/ and refresh CONNECTOR_REGISTRY. Called after resolution_agent writes a new file."""
    global CONNECTOR_REGISTRY
    CONNECTOR_REGISTRY.clear()
    for _path in sorted(Path(__file__).parent.glob("*.py")):
        if _path.stem in ("__init__", "base"):
            continue
        try:
            import sys
            module_name = f"databot_agent.connectors.{_path.stem}"
            if module_name in sys.modules:
                del sys.modules[module_name]
            _mod = importlib.import_module(module_name)
            for _name in dir(_mod):
                _obj = getattr(_mod, _name)
                if (
                    isinstance(_obj, type)
                    and issubclass(_obj, BaseConnector)
                    and _obj is not BaseConnector
                    and _obj.SOURCE_TYPE
                ):
                    CONNECTOR_REGISTRY[_obj.SOURCE_TYPE] = _obj
        except Exception as _e:
            logging.debug(f"connectors: reload skipped {_path.stem}: {_e}")
