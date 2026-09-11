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
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any

from .core import digest

LEGACY_LEDGER_SCHEMA = "ukrainian-llm-eval.shared-spending-ledger.v1"
LEDGER_SCHEMA = "ukrainian-llm-eval.shared-spending-ledger.v2"
RESERVATION_SCHEMA = "ukrainian-llm-eval.shared-spending-reservation.v1"
USAGE_BOUND_RESERVATION_SCHEMA = "ukrainian-llm-eval.shared-spending-reservation.v2"
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
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout = 30000")
        except BaseException:
            connection.close()
            raise
        return connection

    @contextmanager
    def _connection(self):
        """Preserve SQLite transaction behavior while deterministically closing handles."""

        with closing(self._connect()) as connection, connection:
            yield connection

    def _initialize(self) -> None:
        with self._connection() as connection:
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
                "credit_reconciliation_sha256 TEXT, settlement_kind TEXT)"
            )
            row = connection.execute("SELECT schema, ledger_id, cap_micro_usd FROM ledger WHERE singleton=1").fetchone()
            expected = (LEDGER_SCHEMA, self.ledger_id, self.cap_micro_usd)
            if row is None:
                connection.execute(
                    "INSERT INTO ledger(singleton, schema, ledger_id, cap_micro_usd) VALUES(1, ?, ?, ?)",
                    expected,
                )
            elif tuple(row) == (LEGACY_LEDGER_SCHEMA, self.ledger_id, self.cap_micro_usd):
                columns = {
                    item["name"] for item in connection.execute("PRAGMA table_info(reservations)").fetchall()
                }
                if "settlement_kind" not in columns:
                    connection.execute("ALTER TABLE reservations ADD COLUMN settlement_kind TEXT")
                connection.execute(
                    "UPDATE reservations SET settlement_kind='authoritative_account_charge' "
                    "WHERE state='settled' AND settlement_kind IS NULL"
                )
                connection.execute("UPDATE ledger SET schema=? WHERE singleton=1", (LEDGER_SCHEMA,))
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
        with self._connection() as connection:
            connection.execute("BEGIN")
            settled, unresolved = self._totals(connection)
            count = connection.execute("SELECT COUNT(*) FROM reservations").fetchone()[0]
            connection.commit()
        return {
            "schema": LEDGER_SCHEMA,
            "ledger_id": self.ledger_id,
            "cap_micro_usd": self.cap_micro_usd,
            "settled_new_spend_upper_bounds_micro_usd": settled,
            "unresolved_new_spend_micro_usd": unresolved,
            "remaining_new_spend_micro_usd": self.cap_micro_usd - settled - unresolved,
            "reservation_count": int(count),
        }

    @classmethod
    def inspect_readiness(
        cls, path: Path, *, ledger_id: str, cap_micro_usd: int,
    ) -> dict[str, Any]:
        """Validate one existing rollback-journal snapshot without any initialization.

        WAL databases are rejected: a nominally read-only SQLite connection may
        create or update shared-memory sidecars. Immutable mode is deliberately
        not used because it can ignore committed journal state.
        """

        path = Path(path)
        ledger_id = _identifier(ledger_id, "spending ledger ID")
        cap = _integer(cap_micro_usd, "spending cap")
        if not path.is_absolute():
            raise SpendingLedgerError("spending ledger path must be absolute")
        try:
            for entry, mode, directory in ((path.parent, 0o700, True), (path, 0o600, False)):
                status = os.lstat(entry)
                valid_type = stat.S_ISDIR(status.st_mode) if directory else stat.S_ISREG(status.st_mode)
                if (
                    not valid_type or stat.S_IMODE(status.st_mode) != mode
                    or (hasattr(os, "getuid") and status.st_uid != os.getuid())
                    or (not directory and status.st_nlink != 1)
                ):
                    raise SpendingLedgerError("readiness requires an existing private spending ledger")
            with path.open("rb") as source:
                header = source.read(100)
            if len(header) != 100 or header[:16] != b"SQLite format 3\x00":
                raise SpendingLedgerError("corrupt spending ledger header")
            if header[18:20] != b"\x01\x01":
                raise SpendingLedgerError("read-only readiness requires rollback-journal ledger mode")
            with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, isolation_level=None)) as connection:
                connection.row_factory = sqlite3.Row
                connection.execute("BEGIN")
                if connection.execute("PRAGMA journal_mode").fetchone()[0] != "delete":
                    raise SpendingLedgerError("read-only readiness requires rollback-journal ledger mode")
                if [row[0] for row in connection.execute("PRAGMA integrity_check")] != ["ok"]:
                    raise SpendingLedgerError("corrupt spending ledger")
                identity = connection.execute("SELECT * FROM ledger").fetchall()
                if len(identity) != 1 or tuple(identity[0]) != (1, LEDGER_SCHEMA, ledger_id, cap):
                    raise SpendingLedgerError("shared spending ledger identity or cap drift")
                rows = [dict(row) for row in connection.execute(
                    "SELECT reservation_id, binding_sha256, funding_kind, account_sha256, "
                    "maximum_micro_usd, state, settled_micro_usd, settlement_evidence_sha256, "
                    "credit_reconciliation_sha256, settlement_kind FROM reservations ORDER BY reservation_id"
                )]
                settled, unresolved = cls._totals(connection)
                credits: dict[str, int] = {}
                ids: set[str] = set()
                for row in rows:
                    reservation_id = _identifier(row["reservation_id"], "reservation ID")
                    if reservation_id in ids:
                        raise SpendingLedgerError("duplicate shared reservation")
                    ids.add(reservation_id)
                    _sha(row["binding_sha256"], "reservation binding")
                    account = _sha(row["account_sha256"], "account identity")
                    maximum = _integer(row["maximum_micro_usd"], "maximum reservation")
                    if row["funding_kind"] not in {"metered", "existing_credit"}:
                        raise SpendingLedgerError("unsupported shared-ledger funding kind")
                    if row["state"] == "unresolved":
                        if any(row[key] is not None for key in (
                            "settled_micro_usd", "settlement_evidence_sha256",
                            "settlement_kind", "credit_reconciliation_sha256",
                        )):
                            raise SpendingLedgerError("unresolved reservation has settlement data")
                        retained = maximum
                    elif row["state"] == "settled":
                        retained = _integer(row["settled_micro_usd"], "settled charge")
                        if retained > maximum:
                            raise SpendingLedgerError("settled amount exceeds reserved worst case")
                        _sha(row["settlement_evidence_sha256"], "settlement evidence")
                        if row["settlement_kind"] not in {
                            "authoritative_account_charge", "conservative_final_usage_upper_bound",
                        }:
                            raise SpendingLedgerError("unsupported shared reservation settlement kind")
                        if row["credit_reconciliation_sha256"] is not None:
                            _sha(row["credit_reconciliation_sha256"], "credit reconciliation evidence")
                            if row["funding_kind"] != "existing_credit":
                                raise SpendingLedgerError("metered reservation has credit reconciliation")
                            retained = 0
                    else:
                        raise SpendingLedgerError("invalid shared reservation state")
                    if row["funding_kind"] == "existing_credit":
                        credits[account] = credits.get(account, 0) + retained
                if settled + unresolved > cap:
                    raise SpendingLedgerError("shared spending commitments exceed authorized cap")
                return {
                    "schema": LEDGER_SCHEMA, "ledger_id": ledger_id, "cap_micro_usd": cap,
                    "settled_new_spend_upper_bounds_micro_usd": settled,
                    "unresolved_new_spend_micro_usd": unresolved,
                    "remaining_new_spend_micro_usd": cap - settled - unresolved,
                    "reservation_count": len(rows), "reservations_sha256": digest(rows),
                    "credit_commitments_micro_usd": credits,
                }
        except (OSError, sqlite3.Error) as exc:
            raise SpendingLedgerError("existing spending ledger is missing, corrupt, or unreadable") from exc

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
        with self._connection() as connection:
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

    def settle(
        self, reservation_id: str, *, charged_micro_usd: int, evidence_sha256: str,
        settlement_kind: str = "authoritative_account_charge",
    ) -> dict[str, Any]:
        """Replace a worst case with an exact charge or conservative final upper bound."""

        reservation_id = _identifier(reservation_id, "reservation ID")
        charged = _integer(charged_micro_usd, "settled charge")
        evidence = _sha(evidence_sha256, "settlement evidence")
        if settlement_kind not in {
            "authoritative_account_charge", "conservative_final_usage_upper_bound"
        }:
            raise SpendingLedgerError("unsupported shared reservation settlement kind")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM reservations WHERE reservation_id=?", (reservation_id,)).fetchone()
            if row is None:
                raise SpendingLedgerError("unknown shared reservation")
            if charged > row["maximum_micro_usd"]:
                raise SpendingLedgerError("settled amount exceeds reserved worst case")
            if row["state"] == "settled":
                if (
                    row["settled_micro_usd"], row["settlement_evidence_sha256"], row["settlement_kind"]
                ) != (charged, evidence, settlement_kind):
                    raise SpendingLedgerError("shared reservation settlement drift")
                connection.commit()
                return self._receipt(row, replayed=True)
            connection.execute(
                "UPDATE reservations SET state='settled', settled_micro_usd=?, settlement_evidence_sha256=?, "
                "settlement_kind=? "
                "WHERE reservation_id=? AND state='unresolved'",
                (charged, evidence, settlement_kind, reservation_id),
            )
            row = connection.execute("SELECT * FROM reservations WHERE reservation_id=?", (reservation_id,)).fetchone()
            connection.commit()
            return self._receipt(row, replayed=False)

    def reconcile_existing_credit(self, reservation_id: str, *, evidence_sha256: str) -> dict[str, Any]:
        """Mark a settled credit charge reflected in a later authoritative balance."""

        evidence = _sha(evidence_sha256, "credit reconciliation evidence")
        with self._connection() as connection:
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
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM reservations WHERE reservation_id=?", (reservation_id,)).fetchone()
        return None if row is None else self._receipt(row, replayed=True)

    def _receipt(self, row: sqlite3.Row, *, replayed: bool) -> dict[str, Any]:
        body = {
            "schema": (
                USAGE_BOUND_RESERVATION_SCHEMA
                if row["settlement_kind"] == "conservative_final_usage_upper_bound"
                else RESERVATION_SCHEMA
            ),
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
        if row["settlement_kind"] == "conservative_final_usage_upper_bound":
            body["settlement_kind"] = row["settlement_kind"]
        return body | {"reservation_sha256": digest(body), "replayed": replayed}


__all__ = [
    "LEDGER_SCHEMA",
    "LEGACY_LEDGER_SCHEMA",
    "RESERVATION_SCHEMA",
    "USAGE_BOUND_RESERVATION_SCHEMA",
    "SharedSpendingLedger",
    "SpendingCapExceeded",
    "SpendingLedgerError",
]
