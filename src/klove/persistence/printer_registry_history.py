"""Append-only SQLite adapter and bounded reads for registry history."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from typing import Final

from pydantic import ValidationError

from klove.domain.artifacts import SafetyProfile, safety_profile_fingerprint
from klove.domain.onboarding import PrinterIdentityEvidence
from klove.domain.registry_history import (
    MappingHistoryAppend,
    MappingHistoryCursor,
    MappingHistoryRecord,
    ProfileHistoryAppend,
    ProfileHistoryCursor,
    ProfileHistoryEvent,
    ProfileHistoryRecord,
    RegistryHistoryAppendPlan,
    canonical_identity_json,
    canonical_profile_json,
    mapping_fingerprint,
)
from klove.persistence.printer_registry_errors import RegistryStoreError

_MAX_HISTORY_PAGE: Final = 1_000


class SqliteRegistryHistory:
    """Write and read history using a caller-owned registry connection."""

    def retained_profile_generations(
        self,
        connection: sqlite3.Connection,
        printer_uuid: str,
        profile_ids: tuple[str, ...],
    ) -> Mapping[str, int]:
        """Return retained maxima needed to reject generation reuse."""
        if len(profile_ids) != len(set(profile_ids)) or any(
            type(profile_id) is not str or not profile_id for profile_id in profile_ids
        ):
            raise RegistryStoreError
        requested = set(profile_ids)
        rows = connection.execute(
            """
            SELECT slicer_profile_id, MAX(generation)
            FROM registry_profile_history
            WHERE printer_uuid = ?
            GROUP BY slicer_profile_id
            ORDER BY slicer_profile_id
            """,
            (printer_uuid,),
        ).fetchall()
        result: dict[str, int] = {}
        for row in rows:
            if len(row) != 2 or type(row[0]) is not str or type(row[1]) is not int or row[1] < 1:
                raise RegistryStoreError
            if row[0] in requested:
                result[row[0]] = row[1]
        return result

    def append(
        self,
        connection: sqlite3.Connection,
        plan: RegistryHistoryAppendPlan,
    ) -> None:
        """Append one already validated plan without committing its transaction."""
        if plan.mapping is not None:
            self._append_mapping(connection, plan.mapping)
        for profile in plan.profiles:
            self._append_profile(connection, profile)

    @staticmethod
    def _append_mapping(
        connection: sqlite3.Connection,
        record: MappingHistoryAppend,
    ) -> None:
        connection.execute(
            """
            INSERT INTO registry_mapping_history (
                printer_uuid, registry_revision, observed_at_unix_ms,
                mapping_fingerprint, mapping_json
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                record.printer_uuid,
                record.registry_revision,
                record.observed_at_unix_ms,
                record.mapping_fingerprint,
                record.snapshot_json,
            ),
        )

    @staticmethod
    def _append_profile(
        connection: sqlite3.Connection,
        record: ProfileHistoryAppend,
    ) -> None:
        connection.execute(
            """
            INSERT INTO registry_profile_history (
                printer_uuid, registry_revision, slicer_profile_id,
                generation, profile_fingerprint, event, profile_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.printer_uuid,
                record.registry_revision,
                record.slicer_profile_id,
                record.generation,
                record.profile_fingerprint,
                record.event.value,
                record.snapshot_json,
            ),
        )

    def mapping_page(
        self,
        connection: sqlite3.Connection,
        printer_uuid: str,
        *,
        cursor: MappingHistoryCursor | None,
        limit: int,
    ) -> tuple[MappingHistoryRecord, ...]:
        """Return one stable ascending mapping page for an exact printer."""
        _require_limit(limit)
        after_revision = 0
        if cursor is not None:
            if type(cursor) is not MappingHistoryCursor or cursor.registry_revision < 1:
                raise RegistryStoreError
            after_revision = cursor.registry_revision
        rows = connection.execute(
            """
            SELECT printer_uuid, registry_revision, observed_at_unix_ms,
                   mapping_fingerprint, mapping_json
            FROM registry_mapping_history
            WHERE printer_uuid = ? AND registry_revision > ?
            ORDER BY registry_revision
            LIMIT ?
            """,
            (printer_uuid, after_revision, limit),
        ).fetchall()
        return tuple(_decode_mapping(row, printer_uuid) for row in rows)

    def profile_page(
        self,
        connection: sqlite3.Connection,
        printer_uuid: str,
        *,
        cursor: ProfileHistoryCursor | None,
        limit: int,
    ) -> tuple[ProfileHistoryRecord, ...]:
        """Return one stable ascending profile page for an exact printer."""
        _require_limit(limit)
        if cursor is None:
            cursor_values: tuple[object, ...] = (-1, -1, "", -1, "", "")
        else:
            if (
                type(cursor) is not ProfileHistoryCursor
                or cursor.registry_revision < 1
                or type(cursor.slicer_profile_id) is not str
                or not cursor.slicer_profile_id
                or type(cursor.event) is not ProfileHistoryEvent
            ):
                raise RegistryStoreError
            cursor_values = (
                cursor.registry_revision,
                cursor.registry_revision,
                cursor.slicer_profile_id,
                cursor.registry_revision,
                cursor.slicer_profile_id,
                cursor.event.value,
            )
        rows = connection.execute(
            """
            SELECT printer_uuid, registry_revision, slicer_profile_id,
                   generation, profile_fingerprint, event, profile_json
            FROM registry_profile_history
            WHERE printer_uuid = ? AND (
                registry_revision > ?
                OR (registry_revision = ? AND slicer_profile_id > ?)
                OR (registry_revision = ? AND slicer_profile_id = ? AND event > ?)
            )
            ORDER BY registry_revision, slicer_profile_id, event
            LIMIT ?
            """,
            (printer_uuid, *cursor_values, limit),
        ).fetchall()
        return tuple(_decode_profile(row, printer_uuid) for row in rows)


def _require_limit(limit: int) -> None:
    if type(limit) is not int or not 1 <= limit <= _MAX_HISTORY_PAGE:
        raise RegistryStoreError


def _decode_mapping(row: tuple[object, ...], printer_uuid: str) -> MappingHistoryRecord:
    try:
        if (
            len(row) != 5
            or row[0] != printer_uuid
            or type(row[1]) is not int
            or type(row[2]) is not int
            or type(row[3]) is not str
            or type(row[4]) is not str
        ):
            raise RegistryStoreError
        identity = PrinterIdentityEvidence.model_validate_json(row[4])
        if (
            canonical_identity_json(identity) != row[4]
            or identity.observed_at_unix_ms != row[2]
            or mapping_fingerprint(identity) != row[3]
        ):
            raise RegistryStoreError
        return MappingHistoryRecord(
            printer_uuid=printer_uuid,
            registry_revision=row[1],
            observed_at_unix_ms=row[2],
            mapping_fingerprint=row[3],
            identity=identity,
        )
    except RegistryStoreError:
        raise
    except (UnicodeError, ValidationError, ValueError) as exc:
        raise RegistryStoreError from exc


def _decode_profile(row: tuple[object, ...], printer_uuid: str) -> ProfileHistoryRecord:
    try:
        if (
            len(row) != 7
            or row[0] != printer_uuid
            or type(row[1]) is not int
            or type(row[2]) is not str
            or type(row[3]) is not int
            or type(row[4]) is not str
            or type(row[5]) is not str
            or type(row[6]) is not str
        ):
            raise RegistryStoreError
        event = ProfileHistoryEvent(row[5])
        profile = SafetyProfile.model_validate_json(row[6])
        if (
            profile.printer_uuid != printer_uuid
            or profile.slicer_profile_id != row[2]
            or profile.generation != row[3]
            or safety_profile_fingerprint(profile) != row[4]
            or canonical_profile_json(profile) != row[6]
        ):
            raise RegistryStoreError
        return ProfileHistoryRecord(
            printer_uuid=printer_uuid,
            registry_revision=row[1],
            slicer_profile_id=row[2],
            generation=row[3],
            profile_fingerprint=row[4],
            event=event,
            profile=profile,
        )
    except RegistryStoreError:
        raise
    except (UnicodeError, ValidationError, ValueError) as exc:
        raise RegistryStoreError from exc
