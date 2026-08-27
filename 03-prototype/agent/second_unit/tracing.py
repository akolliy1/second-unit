"""Send ADK's own OpenTelemetry traces to Grafana Cloud Agent Observability.

Discovered late and worth the rewrite: **ADK already instruments itself**. It emits spans
with GenAI semantic conventions -- `gen_ai.operation.name`, `gen_ai.usage.input_tokens`,
per-tool spans, per-agent workflow spans -- and Grafana Cloud has the AI Observability app
(`grafana-sigil-app`) enabled to render exactly that.

So we do not need to invent agent telemetry. We need to *export* what ADK already produces,
in the standard conventions, to the partner's product. That is a much stronger integration
than pushing bespoke `second_unit_*` gauges: it is the vendor's own instrumentation and the
vendor's own UI, meeting in the middle on an open standard.

`observe.py` stays alongside this rather than being deleted, because it carries one thing
OTel has no convention for: how many of the agent's *write claims survived independent
verification*. That metric is ours and there is nowhere standard to put it.

Requires `traces:write` on the Grafana Cloud access policy, in addition to the
metrics:write/logs:write the seeder already uses.
"""
import os
from typing import Optional

_STARTED = False


def otlp_endpoint() -> Optional[str]:
    """Grafana Cloud's OTLP gateway for this stack.

    Derived from the Prometheus push URL so there is one fewer thing to configure and get
    wrong: `prometheus-prod-66-prod-us-east-3.grafana.net` -> region `prod-us-east-3` ->
    `https://otlp-gateway-prod-us-east-3.grafana.net/otlp`.
    """
    explicit = os.environ.get("OTLP_ENDPOINT")
    if explicit:
        return explicit.rstrip("/")
    push = os.environ.get("PROM_REMOTE_WRITE_URL", "")
    if "grafana.net" not in push:
        return None
    host = push.split("//", 1)[-1].split("/", 1)[0]          # prometheus-prod-66-prod-us-east-3.grafana.net
    stem = host.split(".grafana.net")[0]
    if "-prod-" not in stem:
        return None
    region = stem.split("-", 2)[-1] if stem.count("-") >= 2 else None
    # prometheus-prod-66-prod-us-east-3 -> prod-us-east-3
    parts = stem.split("-")
    try:
        idx = parts.index("prod", 2)          # the SECOND 'prod' starts the region
        region = "-".join(parts[idx:])
    except ValueError:
        pass
    return f"https://otlp-gateway-{region}.grafana.net/otlp" if region else None


def setup_tracing(service_name: str = "second-unit", verbose: bool = True) -> bool:
    """Wire ADK's spans to Grafana Cloud. Returns True if an exporter was installed.

    Safe to call more than once and safe to fail: tracing must never be the reason an
    investigation does not run.
    """
    global _STARTED
    if _STARTED:
        return True
    if os.environ.get("SECOND_UNIT_TRACING") in ("0", "false", "no"):
        return False

    endpoint = otlp_endpoint()
    user = os.environ.get("OTLP_USER") or os.environ.get("PROM_USER")
    token = os.environ.get("GRAFANA_CLOUD_TOKEN")
    if not (endpoint and user and token):
        if verbose:
            print("   tracing: not configured (need OTLP endpoint, OTLP_USER, token)")
        return False

    try:
        import base64
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        auth = base64.b64encode(f"{user}:{token}".encode()).decode()
        exporter = OTLPSpanExporter(
            endpoint=f"{endpoint}/v1/traces",
            headers={"Authorization": f"Basic {auth}"},
            timeout=20,
        )
        resource = Resource.create({
            "service.name": service_name,
            "service.namespace": "second-unit",
            "deployment.environment": os.environ.get("DEPLOY_ENV", "local"),
        })
        provider = TracerProvider(resource=resource)
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)
        _STARTED = True
        if verbose:
            print(f"   tracing: exporting ADK spans to {endpoint}")
        return True
    except Exception as exc:  # noqa: BLE001, never let telemetry break a run
        if verbose:
            print(f"   tracing: disabled ({type(exc).__name__}: {exc})")
        return False


def flush(timeout_ms: int = 8000) -> None:
    """Force-export before the process exits, or a short CLI run loses its spans."""
    try:
        from opentelemetry import trace
        provider = trace.get_tracer_provider()
        if hasattr(provider, "force_flush"):
            provider.force_flush(timeout_ms)
    except Exception:  # noqa: BLE001
        pass
