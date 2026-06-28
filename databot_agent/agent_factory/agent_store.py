"""
Shared mutable state for dynamic agent creation.

agent.py calls init() at startup to register the root_agent reference and
the model object. factory_tools.py then calls add_agent() at runtime to
append newly created agents to root_agent.sub_agents without needing to
import agent.py (which would be a circular import).
"""

_root_agent = None
_model = None


def init(root_agent, model) -> None:
    """Register the root_agent and model. Called once from agent.py at startup."""
    global _root_agent, _model
    _root_agent = root_agent
    _model = model


def add_agent(new_agent) -> bool:
    """
    Append new_agent to root_agent.sub_agents.
    Returns True if added, False if an agent with that name already exists.
    Raises RuntimeError if init() was never called.
    """
    if _root_agent is None:
        raise RuntimeError("agent_store.init() has not been called yet.")
    existing = {a.name for a in _root_agent.sub_agents}
    if new_agent.name in existing:
        return False
    _root_agent.sub_agents.append(new_agent)
    return True


def get_model():
    """Return the model object set during init()."""
    return _model


def list_live_agent_names() -> list[str]:
    """Return names of all current root_agent sub-agents."""
    if _root_agent is None:
        return []
    return [a.name for a in _root_agent.sub_agents]
