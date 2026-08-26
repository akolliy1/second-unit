"""Rung 0 check: is this machine actually able to run the spike?

Fails loudly and specifically. Every check prints the fix, not just the error.
"""
import os
import sys

from dotenv import load_dotenv

load_dotenv()

OK, BAD = "  ok  ", " FAIL "
failures = []


def check(label, fn, fix):
    try:
        detail = fn()
    except Exception as exc:  # noqa: BLE001 - preflight reports, never raises
        print(f"[{BAD}] {label}: {type(exc).__name__}: {exc}")
        print(f"         fix: {fix}")
        failures.append(label)
    else:
        print(f"[{OK}] {label}{f': {detail}' if detail else ''}")


def py_version():
    if sys.version_info < (3, 10):
        raise RuntimeError(f"running {sys.version.split()[0]}, need >=3.10")
    return sys.version.split()[0]


def adk_imports():
    from google.adk.agents import LlmAgent  # noqa: F401
    from google.adk.tools.mcp_tool import (  # noqa: F401
        McpToolset,
        StreamableHTTPConnectionParams,
    )
    import google.adk

    return f"google-adk {getattr(google.adk, '__version__', 'unknown')}"


def mcp_imports():
    from mcp import ClientSession  # noqa: F401
    from mcp.client.streamable_http import streamablehttp_client  # noqa: F401

    return None


def env_vars():
    required = [
        "GOOGLE_CLOUD_PROJECT",
        "GOOGLE_CLOUD_LOCATION",
        "GRAFANA_URL",
        "GRAFANA_SERVICE_ACCOUNT_TOKEN",
        "MCP_GRAFANA_URL",
        "MCP_GRAFANA_SERVER_TOKEN",
    ]
    missing = [k for k in required if not os.getenv(k) or os.getenv(k, "").startswith("your-")]
    if missing:
        raise RuntimeError("unset/placeholder: " + ", ".join(missing))
    return f"project={os.getenv('GOOGLE_CLOUD_PROJECT')}"


def adc():
    import google.auth

    creds, project = google.auth.default()
    return f"credentials found, project={project}"


def gemini_call():
    """The only check that costs money. Confirms Vertex + model name + quota."""
    from google import genai

    client = genai.Client(
        vertexai=True,
        project=os.environ["GOOGLE_CLOUD_PROJECT"],
        location=os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1"),
    )
    model = os.getenv("GEMINI_MODEL", "gemini-flash-latest")
    resp = client.models.generate_content(model=model, contents="Reply with the single word: rolling")
    return f"{model} -> {(resp.text or '').strip()[:40]}"


print("\n=== Rung 0: preflight ===\n")
check("python >= 3.10", py_version, "run ./bootstrap.sh, then use ./.venv/bin/python")
check("ADK imports", adk_imports, "./.venv/bin/pip install -r requirements.txt")
check("mcp sdk imports", mcp_imports, "./.venv/bin/pip install 'mcp>=1.9.0'")
check("env vars", env_vars, "fill in .env (copy from .env.example)")
check("application default credentials", adc, "gcloud auth application-default login")
check("Vertex AI + Gemini reachable", gemini_call,
      "gcloud services enable aiplatform.googleapis.com  /  check GEMINI_MODEL is a real model id")

print()
if failures:
    print(f"BLOCKED on {len(failures)}: {', '.join(failures)}")
    sys.exit(1)
print("All green. Start ./start_grafana_mcp.sh, then run rung1_mcp_raw.py")
