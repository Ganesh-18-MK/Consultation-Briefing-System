#!/usr/bin/env python3
"""Historically a one-time DB setup step (creating the SQLite schema).
Firestore has no schema to create — collections and fields exist the
moment a document is written — so init_db() is now a no-op kept only so
this script and the deploy docs referencing it don't need to change.
Kept as a quick sanity check: it also confirms the app can reach
Firestore with the project's default credentials before you deploy."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import init_db, _db

if __name__ == "__main__":
    init_db()
    client = _db()
    print(f"Connected to Firestore project: {client.project}")
