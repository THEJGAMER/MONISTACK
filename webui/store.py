"""Persisted storage for devices added through the UI (as opposed to the
statically-configured devices in devices.yaml, which live in env vars).

Backed by the shared SQLite database (db.py) - one row per device, the
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
        rows = self.db.query("SELECT data FROM devices ORDER BY rowid")
        return [json.loads(r["data"]) for r in rows]

    def add(self, device):
        existing = self.db.query_one("SELECT 1 FROM devices WHERE id = ?", (device["id"],))
        if existing is not None:
            raise ValueError(f"device id {device['id']!r} already exists")
        self.db.execute("INSERT INTO devices (id, data) VALUES (?, ?)", (device["id"], json.dumps(device)))
        return device

    def delete(self, device_id):
        cur = self.db.execute("DELETE FROM devices WHERE id = ?", (device_id,))
        return cur.rowcount > 0
