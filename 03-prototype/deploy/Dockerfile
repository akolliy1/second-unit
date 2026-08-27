# Second Unit: one container, two processes.
#
# The architecture doc has the MCP bridge as its own Cloud Run service. Collapsing it into
# this container instead removes a public network hop, a second bearer token, a second cold
# start and a second thing that can be down at 3am when a judge clicks the link. The agent
# talks to the bridge over localhost, which is strictly better than talking to it over the
# internet with a shared secret.
#
# Stage 1 builds the bridge from source rather than copying it out of the published image.
# The first draft did `COPY --from=grafana/mcp-grafana /app/mcp-grafana`, which assumed a
# path inside someone else's image that we never verified -- and Docker is not running here
# to check. `go install` has no such assumption, is pinned to a real version, and also
# avoids Docker Hub pull limits during a Cloud Build.
# `golang:1-bookworm` tracks the latest Go 1.x rather than pinning a minor we would have to
# chase. Pinning 1.24 failed the build: mcp-grafana v1.2.0 requires go >= 1.26.3, and the
# official images set GOTOOLCHAIN=local, which forbids the automatic toolchain upgrade that
# made this work on the laptop. GOTOOLCHAIN=auto restores that behaviour so the module's own
# go directive decides, not the base image's tag.
FROM golang:1-bookworm AS mcp
ENV GOTOOLCHAIN=auto
ARG MCP_GRAFANA_VERSION=v1.2.0
# Retried, because this step reaches out to proxy.golang.org and sum.golang.org for a few
# hundred modules and a single truncated stream fails the whole build. It has already cost
# one deploy: "verifying module: ... stream error: INTERNAL_ERROR; received from peer" on
# a dozen modules at once, which is the proxy having a bad minute, not a broken pin.
# Retrying is the right response to that. Note what is NOT done here: GOSUMDB is left on
# and GOFLAGS untouched, so checksum verification still happens on every attempt. Turning
# it off would make this step pass reliably by no longer checking what it downloaded.
RUN for attempt in 1 2 3 4; do \
      echo "go install attempt ${attempt}"; \
      go install github.com/grafana/mcp-grafana/cmd/mcp-grafana@${MCP_GRAFANA_VERSION} && break; \
      if [ "${attempt}" = "4" ]; then echo "go install failed after 4 attempts"; exit 1; fi; \
      sleep $((attempt * 10)); \
    done \
 && test -x /go/bin/mcp-grafana \
 && echo "built mcp-grafana ${MCP_GRAFANA_VERSION}"

FROM python:3.11-slim AS app

# tini reaps zombies and forwards signals: without an init, Cloud Run's SIGTERM does not
# reach both children and the revision takes the full grace period to shut down.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tini curl \
 && rm -rf /var/lib/apt/lists/* \
 # Assert the path instead of assuming it. The first deploy died on
 # ENTRYPOINT ["/usr/sbin/tini", ...] because Debian installs it to /usr/bin/tini --
 # a container that cannot exec its entrypoint fails the Cloud Run startup probe with
 # no application log at all, which is a miserable thing to debug. Fail the BUILD here
 # if the binary is not where the entrypoint expects it.
 && test -x /usr/bin/tini

COPY --from=mcp /go/bin/mcp-grafana /usr/local/bin/mcp-grafana

WORKDIR /srv
COPY deploy/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy the three packages the service needs. Kept explicit so a stray local file
# (a .env, a venv, someone's notes) cannot ride along into a public image.
COPY agent/second_unit ./second_unit
COPY web/app.py web/live.py web/fixture.json ./
COPY web/templates ./templates
COPY telemetry ./telemetry
COPY deploy/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# Fail the build if the layout is not what the entrypoint assumes.
RUN test -f /srv/app.py && test -d /srv/templates && test -d /srv/second_unit \
 && test -x /usr/local/bin/mcp-grafana \
 && python -c "import fastapi, uvicorn, jinja2, google.adk, mcp; print('deps ok')"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MCP_GRAFANA_URL=http://127.0.0.1:8000/mcp \
    PORT=8080

EXPOSE 8080
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]
