"""A minimal in-memory stand-in for google.cloud.firestore, purpose-built
to support exactly the operations app/db.py uses: document get/set/
update, and collection queries combining zero or more equality/range
`where` filters with an optional order_by and limit.

Why a fake instead of the real Firestore emulator: the emulator needs
Java and the gcloud CLI's emulator component installed locally just to
run `pytest`, which is one more thing to install and keep running on a
non-technical user's machine. This fake needs nothing beyond what's
already in requirements.txt, runs entirely in memory, and is fast/
deterministic — at the cost of not exercising Firestore's real index
requirements (see README's Cloud Run section for the composite indexes
that do need to exist against the real thing).
"""
from __future__ import annotations

from typing import Any, Callable, Optional


class FakeFieldFilter:
    """Stand-in for google.cloud.firestore_v1.base_query.FieldFilter."""

    def __init__(self, field: str, op: str, value: Any):
        self.field = field
        self.op = op
        self.value = value

    def matches(self, data: dict) -> bool:
        actual = data.get(self.field)
        if self.op == "==":
            return actual == self.value
        if self.op == ">=":
            return actual is not None and actual >= self.value
        if self.op == "<=":
            return actual is not None and actual <= self.value
        if self.op == ">":
            return actual is not None and actual > self.value
        if self.op == "<":
            return actual is not None and actual < self.value
        if self.op == "!=":
            return actual != self.value
        raise NotImplementedError(f"FakeFieldFilter doesn't support op {self.op!r}")


class FakeDocumentSnapshot:
    def __init__(self, doc_id: str, data: Optional[dict]):
        self.id = doc_id
        self._data = dict(data) if data is not None else None

    @property
    def exists(self) -> bool:
        return self._data is not None

    def to_dict(self) -> Optional[dict]:
        return dict(self._data) if self._data is not None else None


class FakeDocumentRef:
    def __init__(self, store: dict, collection: str, doc_id: str):
        self._store = store
        self._collection = collection
        self.id = doc_id

    def _bucket(self) -> dict:
        return self._store.setdefault(self._collection, {})

    def get(self) -> FakeDocumentSnapshot:
        return FakeDocumentSnapshot(self.id, self._bucket().get(self.id))

    def set(self, data: dict) -> None:
        self._bucket()[self.id] = dict(data)

    def update(self, data: dict) -> None:
        bucket = self._bucket()
        if self.id not in bucket:
            raise KeyError(f"No such document to update: {self._collection}/{self.id}")
        bucket[self.id].update(data)

    def delete(self) -> None:
        self._bucket().pop(self.id, None)


class FakeQuery:
    def __init__(self, store: dict, collection: str, filters=None, order=None, limit_n=None):
        self._store = store
        self._collection = collection
        self._filters: list[FakeFieldFilter] = list(filters or [])
        self._order = order  # (field, descending: bool) or None
        self._limit = limit_n

    def where(self, field: str, op: str, value: Any) -> "FakeQuery":
        # Matches the real SDK's stable positional where(field, op, value)
        # call form — see app/db.py's import comment for why this project
        # uses that form rather than the newer FieldFilter keyword form.
        return FakeQuery(
            self._store, self._collection, self._filters + [FakeFieldFilter(field, op, value)], self._order, self._limit
        )

    def order_by(self, field: str, direction: Optional[str] = None) -> "FakeQuery":
        descending = direction == "DESCENDING"
        return FakeQuery(self._store, self._collection, self._filters, (field, descending), self._limit)

    def limit(self, n: int) -> "FakeQuery":
        return FakeQuery(self._store, self._collection, self._filters, self._order, n)

    def stream(self):
        bucket = self._store.get(self._collection, {})
        items = [
            (doc_id, data) for doc_id, data in bucket.items()
            if all(f.matches(data) for f in self._filters)
        ]
        if self._order:
            field, descending = self._order
            items.sort(key=lambda kv: kv[1].get(field), reverse=descending)
        if self._limit is not None:
            items = items[: self._limit]
        return [FakeDocumentSnapshot(doc_id, data) for doc_id, data in items]


class FakeCollectionRef(FakeQuery):
    """A collection reference is just a not-yet-filtered query, plus the
    document() accessor real code also uses."""

    def __init__(self, store: dict, collection: str):
        super().__init__(store, collection)

    def document(self, doc_id: Optional[str] = None) -> FakeDocumentRef:
        if doc_id is None:
            bucket = self._store.setdefault(self._collection, {})
            doc_id = f"auto-{len(bucket)}-{id(bucket) % 100000}"
            while doc_id in bucket:
                doc_id = doc_id + "x"
        return FakeDocumentRef(self._store, self._collection, doc_id)


class FakeFirestoreClient:
    """One instance = one isolated in-memory database. Tests get a fresh
    one per test via the temp_db fixture, so no state leaks between
    tests the way it would with a single shared module-level store."""

    def __init__(self):
        self._store: dict[str, dict[str, dict]] = {}

    def collection(self, name: str) -> FakeCollectionRef:
        return FakeCollectionRef(self._store, name)
