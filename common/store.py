"""Persisted storage for devices added through the UI (as opposed to the
statically-configured devices in devices.yaml, which live in env vars).

Backed by the shared Postgres database (db.py) - one row per device, the
device record itself kept as a JSON blob in a `data` column so this store
doesn't need a schema migration every time a new device field is added.
Same threat model as before (and as the .env file): secrets sit in
plaintext on disk, protected by filesystem permissions, not encryption.
"""
import json


class DeviceStore:
    def __init__(self, db):
        self.db = db

    def load(self):
        # SQLite's implicit `rowid` doesn't exist in Postgres - `inserted_at`
        # is the explicit equivalent for "insertion order".
        rows = self.db.query("SELECT data FROM devices ORDER BY inserted_at")
        return [json.loads(r["data"]) for r in rows]

    def add(self, device):
        existing = self.db.query_one("SELECT 1 FROM devices WHERE id = %s", (device["id"],))
        if existing is not None:
            raise ValueError(f"device id {device['id']!r} already exists")
        self.db.execute("INSERT INTO devices (id, data) VALUES (%s, %s)", (device["id"], json.dumps(device)))
        return device

    def delete(self, device_id):
        cur = self.db.execute("DELETE FROM devices WHERE id = %s", (device_id,))
        return cur.rowcount > 0

    def update(self, device_id, device):
        cur = self.db.execute("UPDATE devices SET data = %s WHERE id = %s", (json.dumps(device), device_id))
        if cur.rowcount == 0:
            raise ValueError(f"device id {device_id!r} not found")
        return device

    def get_raw(self, device_id):
        row = self.db.query_one("SELECT data FROM devices WHERE id = %s", (device_id,))
        return json.loads(row["data"]) if row else None
