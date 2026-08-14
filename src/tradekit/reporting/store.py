"""Storage-agnostic persistence for report documents.

The design goal (per the storage requirement) is records that are **NoSQL-native
and trivially archivable to object storage**. Both backends here share the exact
same document/item shape from :mod:`tradekit.reporting.schema`:

* :class:`FileReportStore` — the default local backend. It writes **one JSON
  object per document** at the document's own :meth:`~ReportDocument.archive_path`
  (``record_type/scope/date.json``). That directory tree is a *verbatim* mirror
  of an S3 prefix layout, so cold-archiving is ``aws s3 sync`` with no
  transformation. A JSONL export is provided for bulk load.
* :class:`DynamoReportStore` — maps each document onto a single-table item via
  ``pk`` / ``sk``, the same convention ``falcon-stats`` already uses. Floats are
  coerced to ``Decimal`` for boto3.

Swapping backends never changes the records, only where they live.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Protocol, TextIO

from tradekit.reporting.schema import ReportDocument


def default_store_root() -> Path:
    """Local root for the file store: ``~/.tradekit/reports``."""
    return Path.home() / ".tradekit" / "reports"


class ReportStore(Protocol):
    """Minimal persistence contract shared by every backend."""

    def put(self, doc: ReportDocument) -> None:
        """Insert or replace a document (idempotent on pk+sk)."""
        ...

    def get(self, record_type: str, date: str, scope: str = "GLOBAL") -> dict | None:
        """Fetch a single document item, or ``None`` if absent."""
        ...

    def query(
        self,
        record_type: str,
        scope: str = "GLOBAL",
        since: str | None = None,
        until: str | None = None,
    ) -> list[dict]:
        """Return items of a type within an inclusive ISO-date range, date-sorted."""
        ...


class FileReportStore:
    """One-JSON-object-per-document store mirroring object-storage layout."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root is not None else default_store_root()

    def _path(self, record_type: str, scope: str, date: str) -> Path:
        return self.root / record_type.lower() / scope / f"{date}.json"

    def put(self, doc: ReportDocument) -> None:
        path = self.root / doc.archive_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        # Persist the full NoSQL item (incl. pk/sk/record_type) so each file is
        # self-describing and uploadable to a bucket without re-derivation.
        path.write_text(json.dumps(doc.to_item(), indent=2, sort_keys=True))

    def get(self, record_type: str, date: str, scope: str = "GLOBAL") -> dict | None:
        path = self._path(record_type, scope, date)
        if not path.exists():
            return None
        return json.loads(path.read_text())

    def query(
        self,
        record_type: str,
        scope: str = "GLOBAL",
        since: str | None = None,
        until: str | None = None,
    ) -> list[dict]:
        base = self.root / record_type.lower() / scope
        if not base.exists():
            return []
        items: list[dict] = []
        for path in base.glob("*.json"):
            date = path.stem
            if since is not None and date < since:
                continue
            if until is not None and date > until:
                continue
            items.append(json.loads(path.read_text()))
        items.sort(key=lambda it: it.get("sk", ""))
        return items

    def export_jsonl(self, stream: TextIO, record_type: str, scope: str = "GLOBAL") -> int:
        """Stream all items of a type as JSON Lines (bulk-load / archive format).

        Returns the number of records written.
        """
        n = 0
        for item in self.query(record_type, scope):
            stream.write(json.dumps(item, sort_keys=True))
            stream.write("\n")
            n += 1
        return n


# ── DynamoDB adapter ─────────────────────────────────────────────────────────


def to_dynamo_item(doc: ReportDocument) -> dict:
    """Convert a document to a boto3-ready item (floats → Decimal)."""
    return _floats_to_decimal(doc.to_item())


def from_dynamo_item(item: dict) -> dict:
    """Convert a boto3 item back to JSON-native types (Decimal → int/float)."""
    return _decimal_to_native(item)


def _floats_to_decimal(obj):
    if isinstance(obj, float):
        # str() round-trip avoids binary float artifacts in Decimal.
        return Decimal(str(obj))
    if isinstance(obj, dict):
        return {k: _floats_to_decimal(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_floats_to_decimal(v) for v in obj]
    return obj


def _decimal_to_native(obj):
    if isinstance(obj, Decimal):
        return int(obj) if obj == obj.to_integral_value() else float(obj)
    if isinstance(obj, dict):
        return {k: _decimal_to_native(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_decimal_to_native(v) for v in obj]
    return obj


class DynamoReportStore:
    """DynamoDB single-table backend (``pk`` / ``sk``), lazy boto3 import.

    Query pattern: ``pk = "<RECORD_TYPE>#<scope>"`` with an ``sk`` (ISO date)
    range — the natural DynamoDB access pattern for "all daily cards between
    two dates", which is what makes day-over-day comparison cheap.
    """

    def __init__(self, table_name: str, *, region_name: str | None = None) -> None:
        import boto3  # lazy: keeps the file backend dependency-free at import

        self._table = boto3.resource("dynamodb", region_name=region_name).Table(table_name)

    def put(self, doc: ReportDocument) -> None:
        self._table.put_item(Item=to_dynamo_item(doc))

    def get(self, record_type: str, date: str, scope: str = "GLOBAL") -> dict | None:
        resp = self._table.get_item(Key={"pk": f"{record_type}#{scope}", "sk": date})
        item = resp.get("Item")
        return from_dynamo_item(item) if item else None

    def query(
        self,
        record_type: str,
        scope: str = "GLOBAL",
        since: str | None = None,
        until: str | None = None,
    ) -> list[dict]:
        from boto3.dynamodb.conditions import Key

        cond = Key("pk").eq(f"{record_type}#{scope}")
        if since is not None and until is not None:
            cond = cond & Key("sk").between(since, until)
        elif since is not None:
            cond = cond & Key("sk").gte(since)
        elif until is not None:
            cond = cond & Key("sk").lte(until)
        resp = self._table.query(KeyConditionExpression=cond)
        return [from_dynamo_item(i) for i in resp.get("Items", [])]
