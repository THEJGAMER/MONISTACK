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
# Globs, never a hand-written module list. Naming files individually is
# what shipped a ModuleNotFoundError restart loop here the moment a new
# module was added and missed - the same failure packaging/install-stack.sh
# already carries a comment about, in a second place nobody thought to
# check. Every .py in these directories is meant to ship; the webui's
# tests live in webui/tests/, not at the top level.
COPY common/*.py common/devices.yaml ./
COPY webui/*.py ./
COPY --from=frontend-build /frontend/dist ./frontend/dist
RUN mkdir -p /app/data

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/healthz', timeout=3)" || exit 1
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
