import os
from dotenv import load_dotenv

# Load .env before any environment checks so ANTHROPIC_API_KEY is available.
load_dotenv()

try:
    import google.auth
    _, project_id = google.auth.default()
    os.environ.setdefault("GOOGLE_CLOUD_PROJECT", project_id or "")
    os.environ.setdefault("GOOGLE_CLOUD_LOCATION", "global")
    # Only route through Vertex AI when there is no Anthropic API key.
    # With ANTHROPIC_API_KEY set, ADK uses the Anthropic API directly.
    if os.environ.get("ANTHROPIC_API_KEY"):
        os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "False"
    else:
        os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "True")
except Exception:
    pass

from . import agent  # noqa: F401  — ensures agent.root_agent is importable
