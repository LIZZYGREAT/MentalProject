"""Shared immutable Dataset Snapshot integrity verification."""

from __future__ import annotations

from datetime import date, datetime, timezone
import hashlib
import json
from typing import Any, Iterable
import uuid

from app.models import DatasetSnapshot, DatasetSnapshotItem


SCHEMA_ITEM_COUNTS = {
    "mindflow-research-dataset-v2": (),
    "mindflow-research-dataset-v3": ("participant",),
    "mindflow-research-dataset-v4": (
        "participant",
        "psychometric",
        "daily_review",
        "slow_state",
    ),
    "mindflow-research-dataset-v5": (
        "participant",
        "psychometric",
        "daily_review",
        "slow_state",
        "care_intervention_exposure",
        "warning_delivery",
    ),
    "mindflow-research-dataset-v6": (
        "participant",
        "psychometric",
        "daily_review",
        "slow_state",
        "care_intervention_exposure",
        "warning_delivery",
        "participant_profile",
    ),
}


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=lambda item: item.isoformat()
        if isinstance(item, (date, datetime, uuid.UUID))
        else str(item),
    )


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _utc_iso(value: datetime) -> str:
    return (
        value.replace(tzinfo=timezone.utc)
        if value.tzinfo is None
        else value.astimezone(timezone.utc)
    ).isoformat()


class DatasetSnapshotIntegrityService:
    """Fail closed on schema, manifest, count, membership or hash drift."""

    @staticmethod
    def item_view(row: DatasetSnapshotItem) -> dict[str, Any]:
        return {
            "item_type": row.item_type,
            "source_id": row.source_id,
            "source_version": row.source_version,
            "participant_id": row.participant_id,
            "local_date": row.local_date,
            "source_hash": row.source_hash,
            "metadata": dict(row.metadata_json or {}),
        }

    @staticmethod
    def manifest_hash(
        contract: dict[str, Any], items: list[dict[str, Any]]
    ) -> str:
        canonical_items = sorted(
            [
                {
                    "item_type": item["item_type"],
                    "source_id": item["source_id"],
                    "source_version": item["source_version"],
                    "participant_id": str(item["participant_id"]),
                    "local_date": (
                        item["local_date"].isoformat()
                        if isinstance(item["local_date"], date)
                        else str(item["local_date"])
                    ),
                    "source_hash": item["source_hash"],
                    "metadata": item["metadata"],
                }
                for item in items
            ],
            key=lambda item: (
                item["item_type"],
                item["source_id"],
                item["source_version"],
            ),
        )
        return _hash({"contract": contract, "items": canonical_items})

    def verify(
        self,
        snapshot: DatasetSnapshot,
        rows: Iterable[DatasetSnapshotItem],
        *,
        supported_schema_versions: set[str],
        participant_id: uuid.UUID | None = None,
    ) -> dict[str, Any]:
        schema = str(snapshot.schema_version)
        if schema not in supported_schema_versions or schema not in SCHEMA_ITEM_COUNTS:
            raise ValueError("unsupported dataset schema version")
        items = [self.item_view(row) for row in rows]
        if not items:
            raise ValueError("dataset snapshot has no immutable items")
        manifest = dict(snapshot.manifest_json or {})
        if manifest.get("schema_version") != schema:
            raise ValueError("dataset snapshot schema/manifest mismatch")
        expected_counts = {
            "item_count": len(items),
            "observation_count": sum(
                item["item_type"] == "observation" for item in items
            ),
            "forecast_count": sum(
                item["item_type"] == "forecast" for item in items
            ),
            "calendar_count": sum(
                item["item_type"] == "calendar" for item in items
            ),
        }
        for item_type in SCHEMA_ITEM_COUNTS[schema]:
            expected_counts[f"{item_type}_count"] = sum(
                item["item_type"] == item_type for item in items
            )
        if any(manifest.get(name) != count for name, count in expected_counts.items()):
            raise ValueError("dataset snapshot manifest/items count mismatch")
        participant_ids = {
            item["participant_id"]
            for item in items
            if item["item_type"] == "participant"
        }
        if schema == "mindflow-research-dataset-v2":
            if participant_ids:
                raise ValueError("legacy v2 dataset contains participant membership")
        elif not participant_ids:
            raise ValueError("dataset snapshot has no frozen participant membership")
        if schema != "mindflow-research-dataset-v2":
            item_participant_ids = {item["participant_id"] for item in items}
            if item_participant_ids - participant_ids:
                raise ValueError("dataset snapshot item/membership mismatch")
            membership_codes = {
                str(item["metadata"].get("participant_code") or "").strip()
                for item in items
                if item["item_type"] == "participant"
            }
            requested_codes = {
                str(value).strip()
                for value in (
                    dict(snapshot.participant_filter or {}).get(
                        "participant_codes"
                    )
                    or []
                )
                if str(value).strip()
            }
            if requested_codes and membership_codes != requested_codes:
                raise ValueError("dataset snapshot filter/membership mismatch")
        if participant_id is not None and participant_id not in participant_ids:
            raise ValueError("participant is outside dataset snapshot")
        contract = {
            "schema_version": schema,
            "date_start": snapshot.date_start.isoformat(),
            "date_end": snapshot.date_end.isoformat(),
            "participant_filter": dict(snapshot.participant_filter or {}),
            "observation_cutoff": _utc_iso(snapshot.observation_cutoff),
            "calendar_cutoff": _utc_iso(snapshot.calendar_cutoff),
        }
        calculated_hash = self.manifest_hash(contract, items)
        if calculated_hash != manifest.get("manifest_hash"):
            raise ValueError("dataset snapshot manifest mismatch")
        return {
            "schema_version": schema,
            "manifest_hash": calculated_hash,
            "items": items,
            "participant_ids": participant_ids,
            "expected_counts": expected_counts,
        }
