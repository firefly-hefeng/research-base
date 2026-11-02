"""SQLite catalog, immutable objects and short cross-process library locks."""
from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .models import ResearchError


def now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def uid(prefix: str) -> str:
    return prefix + "_" + uuid.uuid4().hex[:20]


def dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def loads(value, default=None):
    return json.loads(value) if value else default


SCHEMA = """
CREATE TABLE IF NOT EXISTS collections (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL,
 config TEXT NOT NULL DEFAULT '{}', revision INTEGER NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS papers (
 id TEXT PRIMARY KEY, metadata TEXT NOT NULL, preferred_version_id TEXT,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS versions (
 id TEXT PRIMARY KEY, paper_id TEXT NOT NULL REFERENCES papers(id),
 kind TEXT NOT NULL, metadata TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS identifiers (
 namespace TEXT NOT NULL, value TEXT NOT NULL,
 paper_id TEXT NOT NULL REFERENCES papers(id), version_id TEXT REFERENCES versions(id),
 PRIMARY KEY(namespace,value)
);
CREATE TABLE IF NOT EXISTS source_records (
 source TEXT NOT NULL, source_id TEXT NOT NULL, paper_id TEXT NOT NULL,
 version_id TEXT NOT NULL, metadata TEXT NOT NULL, fetched_at TEXT NOT NULL,
 PRIMARY KEY(source,source_id)
);
CREATE TABLE IF NOT EXISTS memberships (
 collection_id TEXT NOT NULL REFERENCES collections(id),
 paper_id TEXT NOT NULL REFERENCES papers(id), status TEXT NOT NULL,
 PRIMARY KEY(collection_id,paper_id)
);
CREATE TABLE IF NOT EXISTS paper_tags (
 paper_id TEXT NOT NULL REFERENCES papers(id), tag TEXT NOT NULL COLLATE NOCASE,
 PRIMARY KEY(paper_id,tag)
);
CREATE TABLE IF NOT EXISTS materials (
 id TEXT PRIMARY KEY, paper_id TEXT NOT NULL REFERENCES papers(id),
 version_id TEXT REFERENCES versions(id), role TEXT NOT NULL, format TEXT NOT NULL,
 status TEXT NOT NULL, parse_status TEXT NOT NULL DEFAULT 'not_started',
 relative_path TEXT, sha256 TEXT, source_url TEXT, original_name TEXT,
 details TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chunks (
 material_id TEXT NOT NULL REFERENCES materials(id), page INTEGER NOT NULL,
 text TEXT NOT NULL, PRIMARY KEY(material_id,page)
);
CREATE VIRTUAL TABLE IF NOT EXISTS search_fts USING fts5(
 paper_id UNINDEXED, material_id UNINDEXED, page UNINDEXED, text,
 tokenize='unicode61'
);
CREATE TABLE IF NOT EXISTS jobs (
 id TEXT PRIMARY KEY, type TEXT NOT NULL, collection_id TEXT NOT NULL,
 state TEXT NOT NULL, payload TEXT NOT NULL, progress TEXT NOT NULL DEFAULT '{}',
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS requests (
 operation TEXT NOT NULL, request_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
 job_id TEXT NOT NULL REFERENCES jobs(id), PRIMARY KEY(operation,request_id)
);
CREATE TABLE IF NOT EXISTS tasks (
 id TEXT PRIMARY KEY, target_key TEXT NOT NULL, paper_id TEXT NOT NULL,
 state TEXT NOT NULL, target TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
 lease_id TEXT, lease_token TEXT, lease_expires REAL, session_id TEXT,
 result TEXT NOT NULL DEFAULT '{}', updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS active_target ON tasks(target_key)
 WHERE state NOT IN ('succeeded','failed','cancelled');
CREATE TABLE IF NOT EXISTS job_tasks (
 job_id TEXT NOT NULL REFERENCES jobs(id), task_id TEXT NOT NULL REFERENCES tasks(id),
 active INTEGER NOT NULL DEFAULT 1, PRIMARY KEY(job_id,task_id)
);
CREATE TABLE IF NOT EXISTS submissions (
 task_id TEXT NOT NULL, submission_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
 response TEXT NOT NULL, PRIMARY KEY(task_id,submission_id)
);
CREATE TABLE IF NOT EXISTS search_runs (
 collection_id TEXT NOT NULL, fingerprint TEXT NOT NULL, completed_at TEXT NOT NULL,
 coverage_through TEXT NOT NULL, PRIMARY KEY(collection_id,fingerprint)
);
CREATE TABLE IF NOT EXISTS discovery_hits (
 job_id TEXT NOT NULL REFERENCES jobs(id), paper_id TEXT NOT NULL REFERENCES papers(id),
 stage TEXT NOT NULL, source TEXT NOT NULL, source_id TEXT NOT NULL, identity TEXT NOT NULL,
 PRIMARY KEY(job_id,stage,source,source_id)
);
CREATE TABLE IF NOT EXISTS citation_edges (
 citing_paper_id TEXT NOT NULL REFERENCES papers(id), cited_paper_id TEXT NOT NULL REFERENCES papers(id),
 provider TEXT NOT NULL, discovered_at TEXT NOT NULL,
 PRIMARY KEY(citing_paper_id,cited_paper_id,provider)
);
CREATE TABLE IF NOT EXISTS paper_relations (
 published_paper_id TEXT NOT NULL REFERENCES papers(id),
 preprint_paper_id TEXT NOT NULL REFERENCES papers(id),
 provider TEXT NOT NULL, verified_at TEXT NOT NULL,
 PRIMARY KEY(published_paper_id,preprint_paper_id)
);
CREATE INDEX IF NOT EXISTS material_paper ON materials(paper_id);
CREATE INDEX IF NOT EXISTS version_paper ON versions(paper_id);
CREATE INDEX IF NOT EXISTS source_paper ON source_records(paper_id);
"""


class Store:
    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "catalog.sqlite"
        with self.connect() as db:
            if db.execute("PRAGMA user_version").fetchone()[0] > 4:
                raise ResearchError("INVALID_ARGUMENT", "Library database was created by a newer version")
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)
            db.execute("PRAGMA user_version=4")
        with self.lock("initialize"):
            meta = self.root / "library.json"
            if not meta.exists():
                atomic_write(meta, dumps({"schema_version": 1, "library_id": uid("lib"), "created_at": now()}))
            record = loads(meta.read_text(encoding="utf-8"))
            if record.get("schema_version") != 1:
                raise ResearchError("INVALID_ARGUMENT", "Unsupported library schema")
            self.library_id = record["library_id"]

    @contextlib.contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @contextlib.contextmanager
    def lock(self, name: str, timeout: float = 5):
        directory = self.root / ".state"
        directory.mkdir(exist_ok=True)
        path = directory / (name + ".lock")
        handle = path.open("a+b")
        handle.seek(0)
        if path.stat().st_size == 0:
            handle.write(b"0")
            handle.flush()
        deadline = time.monotonic() + timeout
        acquired = False
        try:
            while not acquired:
                try:
                    handle.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                except OSError:
                    if time.monotonic() >= deadline:
                        raise ResearchError("LIBRARY_BUSY", "Another worker is using this library", retryable=True)
                    time.sleep(.05)
            yield
        finally:
            if acquired:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()


def atomic_write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temp.open("w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)
