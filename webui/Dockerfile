FROM node:20-slim AS frontend-build
WORKDIR /frontend
COPY frontend/package.json ./
RUN npm install
COPY frontend/ ./
RUN npm run build

FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py commands.py devices.py store.py results_store.py status_poller.py db.py loki_client.py ssh_client.py parsers.py junos_parsers.py opnsense_parsers.py summarize.py settings.py topology.py topology_store.py trending.py metrics.py logging_setup.py scheduler.py compliance.py devices.yaml .
COPY --from=frontend-build /frontend/dist ./frontend/dist
RUN mkdir -p /app/data

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/healthz', timeout=3)" || exit 1
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
