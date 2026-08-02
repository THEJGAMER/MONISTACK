FROM python:3.13-slim

WORKDIR /app
COPY exporter/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# db.py/store.py/devices.py/ssh_client.py are shared with webui (see
# common/ and webui/Dockerfile) - this process reads the same Postgres
# device registry the webui manages, independently, with Postgres as the
# only shared state between the two (no IPC, no shared volume needed).
COPY common/db.py common/store.py common/devices.py common/ssh_client.py common/metrics.py common/junos_parsers.py common/devices.yaml .
COPY exporter/parsers.py exporter/exporter.py .

EXPOSE 9101
CMD ["python", "exporter.py"]
