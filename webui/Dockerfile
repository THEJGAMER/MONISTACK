FROM node:20-slim AS frontend-build
WORKDIR /frontend
COPY webui/frontend/package.json ./
RUN npm install
COPY webui/frontend/ ./
RUN npm run build

FROM python:3.13-slim
WORKDIR /app
COPY webui/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# db.py/store.py/devices.py/ssh_client.py live in common/ - shared with the
# exporter (both processes independently read the same Postgres device
# registry; see common/README or exporter.py's docstring) rather than each
# keeping its own copy, which is exactly what let exporter/ssh_client.py go
# stale (Dell OS9-only, no Junos/OPNsense/private-key support) before this.
COPY common/db.py common/store.py common/devices.py common/ssh_client.py common/metrics.py common/junos_parsers.py common/devices.yaml .
COPY webui/app.py webui/auth.py webui/occurrences.py webui/paging.py webui/commands.py webui/command_history.py webui/results_store.py webui/retention.py webui/sflow_store.py webui/status_poller.py webui/loki_client.py webui/alertmanager_client.py webui/alert_rules.py webui/alert_acks.py webui/audit.py webui/hardware_alerting.py webui/interface_alerting.py webui/parsers.py webui/opnsense_parsers.py webui/summarize.py webui/settings.py webui/topology.py webui/topology_store.py webui/trending.py webui/logging_setup.py webui/scheduler.py webui/compliance.py .
COPY --from=frontend-build /frontend/dist ./frontend/dist
RUN mkdir -p /app/data

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/healthz', timeout=3)" || exit 1
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
