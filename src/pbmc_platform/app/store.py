"""Transactional records: SQLite locally, a dedicated Firestore database in GCP."""
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3


class LocalStore:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.execute("CREATE TABLE IF NOT EXISTS records (key TEXT PRIMARY KEY, value TEXT NOT NULL)")

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=30)
        try:
            yield db
        finally:
            db.close()

    def get(self, key):
        with self.connection() as db:
            row = db.execute("SELECT value FROM records WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def scan(self, prefix):
        with self.connection() as db:
            rows = db.execute("SELECT key,value FROM records WHERE key LIKE ?", (prefix + "/%",)).fetchall()
        return [json.loads(row[1]) for row in rows]

    def atomic(self, keys, change):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                values = {}
                for key in keys:
                    row = db.execute("SELECT value FROM records WHERE key=?", (key,)).fetchone()
                    values[key] = json.loads(row[0]) if row else None
                result, updates = change(values)
                for key, value in updates.items():
                    if key not in keys:
                        raise ValueError("Transaction may only write declared records")
                    if value is None:
                        db.execute("DELETE FROM records WHERE key=?", (key,))
                    else:
                        db.execute("INSERT INTO records VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                                   (key, json.dumps(value, allow_nan=False)))
                db.commit()
                return result
            except BaseException:
                db.rollback()
                raise

    def put(self, key, value):
        return self.atomic([key], lambda _: (value, {key: value}))


class FirestoreStore:
    def __init__(self, project, database="scrna-app"):
        from google.cloud import firestore
        self.client = firestore.Client(project=project, database=database)

    def reference(self, key):
        collection, name = key.split("/", 1)
        if "/" in name:
            raise ValueError("Invalid record key")
        return self.client.collection(collection).document(name)

    def get(self, key):
        return self.reference(key).get().to_dict()

    def scan(self, prefix):
        return [x.to_dict() for x in self.client.collection(prefix).stream()]

    def atomic(self, keys, change):
        from google.cloud import firestore
        @firestore.transactional
        def execute(transaction):
            refs = {k: self.reference(k) for k in keys}
            values = {k: ref.get(transaction=transaction).to_dict() for k, ref in refs.items()}
            result, updates = change(values)
            for key, value in updates.items():
                if key not in refs:
                    raise ValueError("Transaction may only write declared records")
                if value is None:
                    transaction.delete(refs[key])
                else:
                    transaction.set(refs[key], value)
            return result
        return execute(self.client.transaction())

    def put(self, key, value):
        return self.atomic([key], lambda _: (value, {key: value}))

