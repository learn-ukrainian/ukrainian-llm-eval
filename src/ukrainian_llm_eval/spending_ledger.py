"""Atomic shared-account commitments for sequential paid execution.

The ledger is intentionally outside an execution root.  A logical ledger ID and
cap are bound on first use, so a new process or canary cannot obtain a fresh
budget by choosing another output directory.
"""

from __future__ import annotations

import os
import re
import sqlite3
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .core import digest

LEDGER_SCHEMA = "ukrainian-llm-eval.shared-spending-ledger.v1"
RESERVATION_SCHEMA = "ukrainian-llm-eval.shared-spending-reservation.v1"
_IDENTIFIER_RE = re.compile(r"\A[a-z0-9][a-z0-9._-]{0,127}\Z")
_SHA256_RE = re.compile(r"\A[0-9a-f]{64}\Z")


class SpendingLedgerError(ValueError):
    """Shared spending state is invalid or conflicts with the frozen policy."""


class SpendingCapExceeded(SpendingLedgerError):
    """The next immutable reservation does not fit the authorized shared cap."""


def _integer(value: Any, label: str, *, positive: bool = False) -> int:
    if type(value) is not int or value < (1 if positive else 0):
        raise SpendingLedgerError(f"invalid {label}")
    return value


def _identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER_RE.fullmatch(value) is None:
        raise SpendingLedgerError(f"invalid {label}")
    return value


def _require_owner_only_directory(path: Path) -> None:
    """Create or validate the caller-selected trusted ledger directory."""

    try:
        status = os.lstat(path)
    except FileNotFoundError:
        try:
            path.mkdir(mode=0o700, parents=True)
        except FileExistsError:
            pass
        status = os.lstat(path)
    if stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(status.st_mode):
        raise SpendingLedgerError("spending ledger parent must be a private directory")
    if hasattr(os, "getuid") and status.st_uid != os.getuid():
        raise SpendingLedgerError("spending ledger parent must be owned by the current user")
    if stat.S_IMODE(status.st_mode) != 0o700:
        try:
            os.chmod(path, 0o700, follow_symlinks=False)
        except OSError as exc:
            raise SpendingLedgerError("could not make spending ledger parent owner-only") from exc
        status = os.lstat(path)
        if stat.S_IMODE(status.st_mode) != 0o700:
            raise SpendingLedgerError("spending ledger parent must have mode 700")


def _require_owner_only_file(path: Path) -> None:
    """Create the DB inode privately before SQLite can write any contents."""

    flags = os.O_CREAT | os.O_EXCL | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        descriptor = None
    except OSError as exc:
        raise SpendingLedgerError("could not create private spending ledger") from exc
    if descriptor is not None:
        try:
            os.fchmod(descriptor, 0o600)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    status = os.lstat(path)
    if stat.S_ISLNK(status.st_mode) or not stat.S_ISREG(status.st_mode):
        raise SpendingLedgerError("spending ledger must be a private regular file")
    if hasattr(os, "getuid") and status.st_uid != os.getuid():
        raise SpendingLedgerError("spending ledger must be owned by the current user")
    if stat.S_IMODE(status.st_mode) != 0o600:
        raise SpendingLedgerError("spending ledger must have mode 600")
    if getattr(status, "st_nlink", 1) != 1:
        raise SpendingLedgerError("spending ledger must not have additional hard links")


def _sha(value: Any, label: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise SpendingLedgerError(f"invalid {label}")
    return value


class SharedSpendingLedger:
    """A SQLite ledger whose ``BEGIN IMMEDIATE`` transaction owns cap allocation."""

    def __init__(self, path: Path, *, ledger_id: str, cap_micro_usd: int):
        self.path = Path(path)
        self.ledger_id = _identifier(ledger_id, "spending ledger ID")
        self.cap_micro_usd = _integer(cap_micro_usd, "spending cap")
        if not self.path.is_absolute():
            raise SpendingLedgerError("spending ledger path must be absolute")
        _require_owner_only_directory(self.path.parent)
        _require_owner_only_file(self.path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS ledger ("
                "singleton INTEGER PRIMARY KEY CHECK(singleton = 1), schema TEXT NOT NULL, "
                "ledger_id TEXT NOT NULL, cap_micro_usd INTEGER NOT NULL CHECK(cap_micro_usd >= 0))"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS reservations ("
                "reservation_id TEXT PRIMARY KEY, binding_sha256 TEXT NOT NULL, funding_kind TEXT NOT NULL, "
                "account_sha256 TEXT NOT NULL, maximum_micro_usd INTEGER NOT NULL CHECK(maximum_micro_usd >= 0), "
                "state TEXT NOT NULL CHECK(state IN ('unresolved','settled')), "
                "settled_micro_usd INTEGER, settlement_evidence_sha256 TEXT, "
                "credit_reconciliation_sha256 TEXT)"
            )
            row = connection.execute("SELECT schema, ledger_id, cap_micro_usd FROM ledger WHERE singleton=1").fetchone()
            expected = (LEDGER_SCHEMA, self.ledger_id, self.cap_micro_usd)
            if row is None:
                connection.execute(
                    "INSERT INTO ledger(singleton, schema, ledger_id, cap_micro_usd) VALUES(1, ?, ?, ?)",
                    expected,
                )
            elif tuple(row) != expected:
                raise SpendingLedgerError("shared spending ledger identity or cap drift")
            connection.commit()

    @staticmethod
    def _totals(connection: sqlite3.Connection) -> tuple[int, int]:
        settled = connection.execute(
            "SELECT COALESCE(SUM(settled_micro_usd), 0) FROM reservations "
            "WHERE funding_kind='metered' AND state='settled'"
        ).fetchone()[0]
        unresolved = connection.execute(
            "SELECT COALESCE(SUM(maximum_micro_usd), 0) FROM reservations "
            "WHERE funding_kind='metered' AND state='unresolved'"
        ).fetchone()[0]
        return int(settled), int(unresolved)

    def snapshot(self) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN")
            settled, unresolved = self._totals(connection)
            count = connection.execute("SELECT COUNT(*) FROM reservations").fetchone()[0]
            connection.commit()
        return {
            "schema": LEDGER_SCHEMA,
            "ledger_id": self.ledger_id,
            "cap_micro_usd": self.cap_micro_usd,
            "settled_new_spend_micro_usd": settled,
            "unresolved_new_spend_micro_usd": unresolved,
            "remaining_new_spend_micro_usd": self.cap_micro_usd - settled - unresolved,
            "reservation_count": int(count),
        }

    def reserve(
        self,
        reservation_id: str,
        binding: Mapping[str, Any],
        *,
        maximum_micro_usd: int,
        funding_kind: str,
        account_sha256: str,
        credit_available_micro_usd: int | None = None,
    ) -> dict[str, Any]:
        """Atomically retain a whole-segment worst case before its first request."""

        reservation_id = _identifier(reservation_id, "reservation ID")
        maximum = _integer(maximum_micro_usd, "maximum reservation")
        if funding_kind not in {"metered", "existing_credit"}:
            raise SpendingLedgerError("unsupported shared-ledger funding kind")
        account = _sha(account_sha256, "account identity")
        binding_sha256 = digest(dict(binding))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute("SELECT * FROM reservations WHERE reservation_id=?", (reservation_id,)).fetchone()
            if existing is not None:
                expected = (binding_sha256, funding_kind, account, maximum)
                actual = tuple(existing[name] for name in (
                    "binding_sha256", "funding_kind", "account_sha256", "maximum_micro_usd"
                ))
                if actual != expected:
                    raise SpendingLedgerError("shared reservation identity drift")
                connection.commit()
                return self._receipt(existing, replayed=True)
            settled, unresolved = self._totals(connection)
            if funding_kind == "metered" and settled + unresolved + maximum > self.cap_micro_usd:
                connection.rollback()
                raise SpendingCapExceeded("next reservation exceeds the authorized shared new-spend cap")
            if funding_kind == "existing_credit":
                available = _integer(credit_available_micro_usd, "existing-credit balance")
                retained = connection.execute(
                    "SELECT COALESCE(SUM(CASE WHEN state='unresolved' THEN maximum_micro_usd "
                    "ELSE settled_micro_usd END), 0) FROM reservations "
                    "WHERE funding_kind='existing_credit' AND account_sha256=? "
                    "AND (state='unresolved' OR credit_reconciliation_sha256 IS NULL)",
                    (account,),
                ).fetchone()[0]
                if int(retained) + maximum > available:
                    connection.rollback()
                    raise SpendingCapExceeded("next reservation exceeds authoritative existing-credit availability")
            connection.execute(
                "INSERT INTO reservations(reservation_id, binding_sha256, funding_kind, account_sha256, "
                "maximum_micro_usd, state) VALUES(?, ?, ?, ?, ?, 'unresolved')",
                (reservation_id, binding_sha256, funding_kind, account, maximum),
            )
            row = connection.execute("SELECT * FROM reservations WHERE reservation_id=?", (reservation_id,)).fetchone()
            connection.commit()
            return self._receipt(row, replayed=False)

    def settle(self, reservation_id: str, *, charged_micro_usd: int, evidence_sha256: str) -> dict[str, Any]:
        """Release unused worst case only from authoritative final charge evidence."""

        reservation_id = _identifier(reservation_id, "reservation ID")
        charged = _integer(charged_micro_usd, "settled charge")
        evidence = _sha(evidence_sha256, "settlement evidence")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM reservations WHERE reservation_id=?", (reservation_id,)).fetchone()
            if row is None:
                raise SpendingLedgerError("unknown shared reservation")
            if charged > row["maximum_micro_usd"]:
                raise SpendingLedgerError("authoritative charge exceeds reserved worst case")
            if row["state"] == "settled":
                if (row["settled_micro_usd"], row["settlement_evidence_sha256"]) != (charged, evidence):
                    raise SpendingLedgerError("shared reservation settlement drift")
                connection.commit()
                return self._receipt(row, replayed=True)
            connection.execute(
                "UPDATE reservations SET state='settled', settled_micro_usd=?, settlement_evidence_sha256=? "
                "WHERE reservation_id=? AND state='unresolved'",
                (charged, evidence, reservation_id),
            )
            row = connection.execute("SELECT * FROM reservations WHERE reservation_id=?", (reservation_id,)).fetchone()
            connection.commit()
            return self._receipt(row, replayed=False)

    def reconcile_existing_credit(self, reservation_id: str, *, evidence_sha256: str) -> dict[str, Any]:
        """Mark a settled credit charge reflected in a later authoritative balance."""

        evidence = _sha(evidence_sha256, "credit reconciliation evidence")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM reservations WHERE reservation_id=?", (reservation_id,)).fetchone()
            if row is None or row["funding_kind"] != "existing_credit" or row["state"] != "settled":
                raise SpendingLedgerError("only settled existing-credit reservations can be reconciled")
            current = row["credit_reconciliation_sha256"]
            if current is not None and current != evidence:
                raise SpendingLedgerError("existing-credit reconciliation drift")
            if current is None:
                connection.execute(
                    "UPDATE reservations SET credit_reconciliation_sha256=? WHERE reservation_id=?",
                    (evidence, reservation_id),
                )
            row = connection.execute("SELECT * FROM reservations WHERE reservation_id=?", (reservation_id,)).fetchone()
            connection.commit()
            return self._receipt(row, replayed=current is not None)

    def get(self, reservation_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM reservations WHERE reservation_id=?", (reservation_id,)).fetchone()
        return None if row is None else self._receipt(row, replayed=True)

    def _receipt(self, row: sqlite3.Row, *, replayed: bool) -> dict[str, Any]:
        body = {
            "schema": RESERVATION_SCHEMA,
            "ledger_id": self.ledger_id,
            "reservation_id": row["reservation_id"],
            "binding_sha256": row["binding_sha256"],
            "funding_kind": row["funding_kind"],
            "account_sha256": row["account_sha256"],
            "maximum_micro_usd": row["maximum_micro_usd"],
            "state": row["state"],
            "settled_micro_usd": row["settled_micro_usd"],
            "settlement_evidence_sha256": row["settlement_evidence_sha256"],
            "credit_reconciliation_sha256": row["credit_reconciliation_sha256"],
        }
        return body | {"reservation_sha256": digest(body), "replayed": replayed}


__all__ = [
    "LEDGER_SCHEMA",
    "RESERVATION_SCHEMA",
    "SharedSpendingLedger",
    "SpendingCapExceeded",
    "SpendingLedgerError",
]
