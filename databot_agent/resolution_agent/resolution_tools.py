"""
Resolution Agent tools — autonomous connector code generation and validation.

Scope is intentionally narrow:
  - ONLY writes to databot_agent/connectors/<source_type>.py
  - Generated class MUST extend BaseConnector (validated before accepting)
  - Connection is tested before the file is accepted
  - On failure the LLM reads the error and retries (root_agent enforces max retries)
"""

import importlib
import logging
import sys
from pathlib import Path

from google.adk.tools.tool_context import ToolContext

_CONNECTORS_DIR = Path(__file__).parents[1] / "connectors"

logger = logging.getLogger(__name__)

_REQUIRED_ATTRIBUTES = [
    "SOURCE_TYPE",
    "DISPLAY_NAME",
    "DESCRIPTION",
    "CAPABILITIES",
    "REQUIRED_CREDENTIAL_FIELDS",
]

_REQUIRED_METHODS = [
    "test_connection(cls, credentials: dict) -> dict",
    "query(cls, credentials: dict, query_params: dict) -> dict",
]


def get_connector_interface(tool_context: ToolContext) -> dict:
    """
    Return the full BaseConnector source code, a concrete example implementation,
    required attributes/methods, rules, and the target write path.

    The LLM uses this context to write a correct connector implementation.
    """
    base_path = _CONNECTORS_DIR / "base.py"
    example_path = _CONNECTORS_DIR / "salesforce.py"

    try:
        base_source = base_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        base_source = f"ERROR: {base_path} not found. Cannot provide base class source."
        logger.error("base.py not found at %s", base_path)

    try:
        example_source = example_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        example_source = (
            f"ERROR: {example_path} not found. No example implementation available."
        )
        logger.warning("salesforce.py not found at %s", example_path)

    return {
        "base_class_source": base_source,
        "example_implementation": example_source,
        "required_class_attributes": _REQUIRED_ATTRIBUTES,
        "required_methods": _REQUIRED_METHODS,
        "rules": [
            "Use try/except ImportError for any third-party package. Return status='missing_package' with install_command if missing.",
            "query() must return {status, data, row_count}",
            "test_connection() must return {status, message}",
            "Only READ operations in query() — never write/mutate the source",
        ],
        "write_to": "databot_agent/connectors/<source_type>.py",
    }


def write_connector_file(
    tool_context: ToolContext, source_type: str, code: str
) -> dict:
    """
    Validate and write a connector implementation to the connectors directory.

    Steps:
      1. Normalise source_type (lowercase, hyphens -> underscores)
      2. Syntax-check the code via compile()
      3. Write the file
      4. Force-reimport the module
      5. Find the class that extends BaseConnector
      6. Validate required class attributes
      7. On any failure: delete the file and return an error with fix instructions
      8. On success: return confirmation and next step
    """
    # --- 1. Normalise source_type ---
    source_type = source_type.strip().lower().replace("-", "_")

    target_path = _CONNECTORS_DIR / f"{source_type}.py"
    module_name = f"databot_agent.connectors.{source_type}"

    # --- 2. Compile / syntax check ---
    try:
        compile(code, str(target_path), "exec")
    except SyntaxError as exc:
        return {
            "status": "error",
            "error": f"SyntaxError: {exc}",
            "fix_instruction": (
                f"The generated code has a syntax error at line {exc.lineno}: {exc.msg}. "
                "Fix the syntax and call write_connector_file again with corrected code."
            ),
        }

    # --- 3. Write the file ---
    try:
        _CONNECTORS_DIR.mkdir(parents=True, exist_ok=True)
        target_path.write_text(code, encoding="utf-8")
        logger.info("Wrote connector file: %s", target_path)
    except OSError as exc:
        return {
            "status": "error",
            "error": f"Failed to write file: {exc}",
            "fix_instruction": "Check directory permissions and try again.",
        }

    # --- 4. Force-reimport ---
    sys.modules.pop(module_name, None)
    try:
        module = importlib.import_module(module_name)
    except Exception as exc:  # noqa: BLE001
        _safe_delete(target_path)
        return {
            "status": "error",
            "error": f"Import error after writing file: {exc}",
            "fix_instruction": (
                "The module could not be imported. Fix the import errors "
                "(e.g. missing top-level imports, name errors) and call "
                "write_connector_file again."
            ),
        }

    # --- 5. Locate BaseConnector subclass ---
    try:
        from databot_agent.connectors.base import BaseConnector  # noqa: PLC0415
    except ImportError as exc:
        _safe_delete(target_path)
        return {
            "status": "error",
            "error": f"Could not import BaseConnector: {exc}",
            "fix_instruction": "Ensure databot_agent/connectors/base.py exists and is importable.",
        }

    connector_cls = None
    import inspect  # noqa: PLC0415

    for _name, obj in inspect.getmembers(module, inspect.isclass):
        if issubclass(obj, BaseConnector) and obj is not BaseConnector:
            connector_cls = obj
            break

    if connector_cls is None:
        _safe_delete(target_path)
        return {
            "status": "error",
            "error": "No class extending BaseConnector found in the generated file.",
            "fix_instruction": (
                "Ensure the generated file contains exactly one class that "
                "inherits from BaseConnector. Check the base class import and "
                "class definition, then call write_connector_file again."
            ),
        }

    # --- 6. Validate required class attributes ---
    missing_attrs = [
        attr for attr in _REQUIRED_ATTRIBUTES if not hasattr(connector_cls, attr)
    ]
    if missing_attrs:
        _safe_delete(target_path)
        return {
            "status": "error",
            "error": f"Connector class is missing required attributes: {missing_attrs}",
            "fix_instruction": (
                f"Add the missing class-level attributes {missing_attrs} to the "
                "connector class and call write_connector_file again."
            ),
        }

    # --- 7. All checks passed ---
    return {
        "status": "written",
        "file_path": str(target_path),
        "connector_class": connector_cls.__name__,
        "next_step": "Call test_connector() with the source_type",
    }


def test_connector(tool_context: ToolContext, source_type: str) -> dict:
    """
    Instantiate the freshly written connector and test its connection.

    Credentials are read from tool_context.state["pending_credentials"].
    On success the connector is marked validated in state so root_agent can
    proceed with registration via agent_factory_agent.
    """
    # --- Normalise ---
    source_type = source_type.strip().lower().replace("-", "_")
    module_name = f"databot_agent.connectors.{source_type}"

    # --- Read credentials from state ---
    credentials: dict = tool_context.state.get("pending_credentials", {})
    if not credentials:
        return {
            "status": "error",
            "error": (
                "No credentials found in tool_context.state['pending_credentials']. "
                "Ensure credentials are stored in state before calling test_connector."
            ),
            "action": (
                "Ask root_agent to populate pending_credentials in state, "
                "then call test_connector again."
            ),
        }

    # --- Force-reimport ---
    sys.modules.pop(module_name, None)
    try:
        module = importlib.import_module(module_name)
    except Exception as exc:  # noqa: BLE001
        return {
            "status": "error",
            "error": f"Could not import connector module '{module_name}': {exc}",
            "action": (
                "Fix the connector code and call write_connector_file again, then re-test."
            ),
        }

    # --- Locate connector class ---
    try:
        from databot_agent.connectors.base import BaseConnector  # noqa: PLC0415
    except ImportError as exc:
        return {
            "status": "error",
            "error": f"Could not import BaseConnector: {exc}",
            "action": "Ensure databot_agent/connectors/base.py exists.",
        }

    import inspect  # noqa: PLC0415

    connector_cls = None
    for _name, obj in inspect.getmembers(module, inspect.isclass):
        if issubclass(obj, BaseConnector) and obj is not BaseConnector:
            connector_cls = obj
            break

    if connector_cls is None:
        return {
            "status": "error",
            "error": f"No BaseConnector subclass found in module '{module_name}'.",
            "action": (
                "Fix the connector code and call write_connector_file again, then re-test."
            ),
        }

    # --- Call test_connection ---
    try:
        test_result: dict = connector_cls.test_connection(credentials)
    except Exception as exc:  # noqa: BLE001
        logger.exception("test_connection raised an unexpected exception")
        test_result = {
            "status": "error",
            "message": f"test_connection raised an exception: {exc}",
        }

    success = test_result.get("status") == "success"

    if success:
        tool_context.state[f"{source_type}_connector_validated"] = True
        logger.info("Connector '%s' validated successfully.", source_type)
        action = (
            "Connector validated. Return to root_agent to complete registration "
            "via agent_factory_agent."
        )
    else:
        logger.warning("Connector '%s' failed validation: %s", source_type, test_result)
        action = (
            "Fix the connector code and call write_connector_file again, then re-test."
        )

    return {
        "test_result": test_result,
        "connector_class": connector_cls.__name__,
        "action": action,
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _safe_delete(path: Path) -> None:
    """Delete a file without raising if it does not exist."""
    try:
        path.unlink(missing_ok=True)
        logger.debug("Deleted file: %s", path)
    except OSError as exc:
        logger.warning("Could not delete %s: %s", path, exc)
