FROM python:3.13-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY ssh_client.py parsers.py exporter.py .

EXPOSE 9101
CMD ["python", "exporter.py"]
