from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .security import sanitize

MASTER_ROUTE_KEY = "__master__"
WAKEABLE_STATES = frozenset({
    "ASSIGNED", "RUNNING", "BLOCKED", "WAITING_FOR_USER", "READY", "ERROR", "STALLED"
})
TERMINAL_DELIVERY = frozenset({"UNCERTAIN", "BLOCKED", "DELIVERED"})
CAPABILITY_ENV = "ACC_WAKE_ALL_ENABLED"
MASTER_REF = os.environ.get("ACC_MASTER_REF", "nicofroeba16-cell/ha-grok-bridge#3")


class WakeAllError(RuntimeError):
    pass


def capability_enabled() -> bool:
    return os.environ.get(CAPABILITY_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def _message_id(idempotency_key: str, worker_key: str, goal_version: str) -> str:
    batch = hashlib.sha256(idempotency_key.encode()).hexdigest()[:20]
    item = hashlib.sha256(f"{worker_key}\0{goal_version}".encode()).hexdigest()[:20]
    return f"control-center-wake:{batch}:{item}"


def _validate_route(config: Any) -> tuple[str | None, str | None, str]:
    if not isinstance(config, dict):
        return None, "route_config_invalid", "unknown"
    raw_url = str(config.get("url") or "").strip()
    raw_title = str(config.get("title") or "").strip()
    if bool(raw_url) == bool(raw_title):
        return None, "route_ambiguous", "unknown"
    if raw_url:
        parsed = urlparse(raw_url)
        path = parsed.path.rstrip("/")
        if (
            parsed.scheme != "https"
            or parsed.hostname != "chatgpt.com"
            or parsed.username
            or parsed.password
            or parsed.port
            or not re.fullmatch(r"/(?:g/g-p-[A-Za-z0-9_-]+/)?c/[A-Za-z0-9-]+", path)
        ):
            return None, "route_invalid", "url"
        return raw_url, None, "url"
    if "\n" in raw_title or "\r" in raw_title or len(raw_title) > 200:
        return None, "route_invalid", "title"
    title = " ".join(raw_title.split()).strip()
    if not title:
        return None, "route_invalid", "title"
    return f"chat-title:{title}", None, "title"


def _load_routes(path: Path) -> tuple[dict[str, dict[str, Any]], list[str]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WakeAllError("canonical_route_registry_unavailable") from exc
    if not isinstance(raw, dict):
        raise WakeAllError("canonical_route_registry_invalid")

    routes: dict[str, dict[str, Any]] = {}
    destinations: dict[str, list[str]] = {}
    for raw_key, config in raw.items():
        key = str(raw_key)
        destination, error, kind = _validate_route(config)
        routes[key] = {
            "worker_key": key,
            "destination": destination,
            "route_kind": kind,
            "route_error": error,
        }
        if destination:
            destinations.setdefault(destination, []).append(key)

    ambiguous: list[str] = []
    for keys in destinations.values():
        if len(keys) > 1:
            ambiguous.extend(keys)
            for key in keys:
                routes[key]["route_error"] = "route_destination_ambiguous"
                routes[key]["destination"] = None
    return routes, sorted(set(ambiguous))


def _connect(path: Path, *, writable: bool) -> sqlite3.Connection:
    if not path.is_file():
        raise WakeAllError("canonical_wake_ledger_unavailable")
    mode = "rw" if writable else "ro"
    try:
        conn = sqlite3.connect(f"file:{path}?mode={mode}", uri=True, timeout=3)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error as exc:
        raise WakeAllError("canonical_wake_ledger_unavailable") from exc
    required = {"browser_wake_delivery", "browser_wake_pending_worker"}
    present = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN (?,?)",
            tuple(required),
        ).fetchall()
    }
    if present != required:
        conn.close()
        raise WakeAllError("canonical_wake_schema_unavailable")
    return conn


def _delivery_state(conn: sqlite3.Connection) -> tuple[dict[str, dict[str, Any]], dict[str, set[str]]]:
    latest: dict[str, dict[str, Any]] = {}
    for row in conn.execute(
        """SELECT message_id,route_key,status,attempts,last_error,updated_at
             FROM browser_wake_delivery ORDER BY updated_at DESC"""
    ).fetchall():
        key = str(row["route_key"])
        latest.setdefault(
            key,
            {
                "message_id": str(row["message_id"]),
                "status": str(row["status"] or "").upper(),
                "attempts": int(row["attempts"] or 0),
            },
        )
    pending: dict[str, set[str]] = {}
    for row in conn.execute(
        "SELECT message_id,route_key FROM browser_wake_pending_worker"
    ).fetchall():
        pending.setdefault(str(row["route_key"]), set()).add(str(row["message_id"]))
    return latest, pending


def _public_worker(worker: dict[str, Any], *, reason: str | None = None) -> dict[str, Any]:
    return sanitize(
        {
            "worker_key": worker.get("worker_key"),
            "project": worker.get("project"),
            "chat": worker.get("chat"),
            "goal_version": worker.get("goal_version"),
            "state": worker.get("resolved_state") or worker.get("state") or "UNKNOWN",
            "skip_reason": reason,
        }
    )


_REASON_TEXT = {
    "master_route_excluded": "Master route is never part of Wake all.",
    "missing_route": "No canonical Browser-Wake route is registered.",
    "route_config_invalid": "Route registry entry is malformed.",
    "route_ambiguous": "Route must define exactly one destination kind.",
    "route_destination_ambiguous": "Multiple registry keys resolve to the same destination.",
    "route_invalid": "Canonical route validation failed.",
    "worker_identity_ambiguous": "Multiple current worker rows use the same worker key.",
    "done_without_changed_goal_proof": "DONE is not reactivated without proof of a materially changed goal.",
    "state_not_wakeable": "Current worker state is not wake-eligible.",
    "pending_delivery_exists": "A canonical wake is already pending for this worker.",
    "uncertain_delivery_exists": "Latest canonical delivery is UNCERTAIN and is never auto-retried.",
    "delivery_in_flight": "A canonical delivery is already in flight.",
    "worker_not_registered": "Route has no current worker row.",
}


def reason_text(code: str) -> str:
    return _REASON_TEXT.get(code, code.replace("_", " "))


class WakeAllService:
    """Guarded adapter that queues only into the canonical Browser-Wake pending table."""

    def __init__(
        self,
        *,
        wake_db: Path,
        routes_file: Path,
        worker_provider,
        enabled: bool | None = None,
    ):
        self.wake_db = Path(wake_db)
        self.routes_file = Path(routes_file)
        self.worker_provider = worker_provider
        self.enabled = capability_enabled() if enabled is None else bool(enabled)

    def _snapshot(self, *, replay_key: str | None = None) -> dict[str, Any]:
        if not self.enabled:
            return {
                "enabled": False,
                "available": False,
                "can_submit": False,
                "reason": "capability_disabled",
                "eligible": [],
                "skipped": [],
                "eligible_count": 0,
                "skipped_count": 0,
                "routed_count": 0,
                "orphan_route_count": 0,
            }

        routes, _ = _load_routes(self.routes_file)
        conn = _connect(self.wake_db, writable=False)
        try:
            latest, pending = _delivery_state(conn)
        finally:
            conn.close()

        worker_rows = list(self.worker_provider())
        key_counts: dict[str, int] = {}
        for row in worker_rows:
            key = str(row.get("worker_key") or "")
            key_counts[key] = key_counts.get(key, 0) + 1

        eligible: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        matched: set[str] = set()

        for worker in worker_rows:
            key = str(worker.get("worker_key") or "")
            matched.add(key)
            reason = None
            route = routes.get(key)
            state = str(worker.get("resolved_state") or worker.get("state") or "UNKNOWN").upper()
            goal = str(worker.get("goal_version") or "")

            if key == MASTER_ROUTE_KEY:
                reason = "master_route_excluded"
            elif key_counts.get(key, 0) != 1:
                reason = "worker_identity_ambiguous"
            elif route is None:
                reason = "missing_route"
            elif route.get("route_error"):
                reason = str(route["route_error"])
            elif state == "DONE":
                reason = "done_without_changed_goal_proof"
            elif state not in WAKEABLE_STATES:
                reason = "state_not_wakeable"
            else:
                same_message = _message_id(replay_key, key, goal) if replay_key else None
                pending_ids = pending.get(key, set())
                foreign_pending = {x for x in pending_ids if x != same_message}
                delivery = latest.get(key)
                if foreign_pending:
                    reason = "pending_delivery_exists"
                elif delivery and delivery["status"] == "UNCERTAIN" and delivery["message_id"] != same_message:
                    reason = "uncertain_delivery_exists"
                elif delivery and delivery["status"] == "IN_FLIGHT" and delivery["message_id"] != same_message:
                    reason = "delivery_in_flight"

            if reason:
                item = _public_worker(worker, reason=reason)
                item["skip_reason_text"] = reason_text(reason)
                skipped.append(item)
            else:
                item = _public_worker(worker)
                item["route_kind"] = route["route_kind"]
                eligible.append(item)

        orphan_routes = [
            key for key in routes
            if key != MASTER_ROUTE_KEY and key not in matched
        ]
        for key in orphan_routes:
            skipped.append({
                "worker_key": sanitize(key),
                "project": None,
                "chat": None,
                "goal_version": None,
                "state": "UNREGISTERED",
                "skip_reason": "worker_not_registered",
                "skip_reason_text": reason_text("worker_not_registered"),
            })
        fingerprint_payload = {
            "eligible": [
                [
                    x.get("worker_key"),
                    x.get("goal_version"),
                    x.get("state"),
                    x.get("route_kind"),
                    hashlib.sha256(
                        str((routes.get(str(x.get("worker_key") or "")) or {}).get("destination") or "").encode()
                    ).hexdigest(),
                ]
                for x in eligible
            ],
            "skipped": [
                [x.get("worker_key"), x.get("goal_version"), x.get("state"), x.get("skip_reason")]
                for x in skipped
            ],
            "orphan_route_count": len(orphan_routes),
        }
        preview_hash = hashlib.sha256(
            json.dumps(fingerprint_payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return {
            "enabled": True,
            "available": True,
            "can_submit": bool(eligible),
            "reason": None,
            "eligible": eligible,
            "skipped": skipped,
            "eligible_count": len(eligible),
            "skipped_count": len(skipped),
            "routed_count": len([key for key in routes if key != MASTER_ROUTE_KEY]),
            "orphan_route_count": len(orphan_routes),
            "preview_hash": preview_hash,
        }

    def preview(self) -> dict[str, Any]:
        try:
            result = self._snapshot()
        except WakeAllError as exc:
            return {
                "enabled": self.enabled,
                "available": False,
                "can_submit": False,
                "reason": str(exc),
                "eligible": [],
                "skipped": [],
                "eligible_count": 0,
                "skipped_count": 0,
                "routed_count": 0,
                "orphan_route_count": 0,
            }
        if result["enabled"]:
            result["idempotency_key"] = secrets.token_urlsafe(24)
        return sanitize(result)

    def submit(
        self,
        *,
        idempotency_key: str,
        preview_hash: str,
        confirmed: bool,
    ) -> dict[str, Any]:
        if not self.enabled:
            raise WakeAllError("capability_disabled")
        if not confirmed:
            raise WakeAllError("explicit_confirmation_required")
        if not re.fullmatch(r"[A-Za-z0-9_-]{20,120}", idempotency_key or ""):
            raise WakeAllError("invalid_idempotency_key")
        if not re.fullmatch(r"[0-9a-f]{64}", preview_hash or ""):
            raise WakeAllError("invalid_preview_hash")

        snapshot = self._snapshot(replay_key=idempotency_key)
        # An idempotent replay can legitimately differ from the original preview only
        # because this same batch is now pending/delivered. Detect it below before
        # enforcing the preview hash.
        batch_prefix = "control-center-wake:" + hashlib.sha256(idempotency_key.encode()).hexdigest()[:20] + ":"
        conn = _connect(self.wake_db, writable=True)
        try:
            prior_pending = {
                str(r["message_id"])
                for r in conn.execute(
                    "SELECT message_id FROM browser_wake_pending_worker WHERE message_id LIKE ?",
                    (batch_prefix + "%",),
                ).fetchall()
            }
            prior_delivery = {
                str(r["message_id"]): str(r["status"] or "").upper()
                for r in conn.execute(
                    "SELECT message_id,status FROM browser_wake_delivery WHERE message_id LIKE ?",
                    (batch_prefix + "%",),
                ).fetchall()
            }
            is_replay = bool(prior_pending or prior_delivery)
            if not is_replay and snapshot.get("preview_hash") != preview_hash:
                raise WakeAllError("preview_changed_refresh_required")

            routes, _ = _load_routes(self.routes_file)
            results: list[dict[str, Any]] = []
            skipped = list(snapshot["skipped"])
            conn.execute("BEGIN IMMEDIATE")
            try:
                for worker in snapshot["eligible"]:
                    key = str(worker.get("worker_key") or "")
                    goal = str(worker.get("goal_version") or "")
                    route = routes.get(key) or {}
                    destination = route.get("destination")
                    if not destination:
                        results.append({**worker, "outcome": "skipped", "reason": "route_unavailable_at_submit"})
                        continue
                    message_id = _message_id(idempotency_key, key, goal)
                    delivery = conn.execute(
                        "SELECT status FROM browser_wake_delivery WHERE message_id=?",
                        (message_id,),
                    ).fetchone()
                    if delivery:
                        status = str(delivery[0] or "").upper()
                        if status == "DELIVERED":
                            outcome = "verified"
                        elif status == "UNCERTAIN":
                            outcome = "uncertain"
                        elif status == "BLOCKED":
                            outcome = "blocked"
                        elif status == "FAILED_PRE_SEND":
                            outcome = "failed-pre-send"
                        elif status == "IN_FLIGHT":
                            outcome = "queued"
                        else:
                            outcome = "skipped"
                        results.append({**worker, "outcome": outcome, "reason": f"idempotent_{status.lower()}"})
                        continue

                    existing = conn.execute(
                        "SELECT 1 FROM browser_wake_pending_worker WHERE message_id=?",
                        (message_id,),
                    ).fetchone()
                    if existing:
                        results.append({**worker, "outcome": "queued", "deduplicated": True})
                        continue

                    payload = "\n".join([
                        "WORKER_WAKE",
                        f"WAKE_ID: {message_id}",
                        f"PROJECT: {worker.get('project') or ''}",
                        f"CHAT: {worker.get('chat') or ''}",
                        f"GOAL_VERSION: {goal}",
                        f"MASTER: {MASTER_REF}",
                        "ACTION: Resume the current assigned goal for this canonical worker. Read the latest matching GOAL in Master and the assigned workstream, then continue only that scope.",
                        "LOOP_GUARD: Report substantive progress only through the canonical GitHub workstream/Master status; do not create wake/status echoes.",
                        "LIVE_GATE: Every live-system mutation still requires separate explicit user approval.",
                    ])
                    cur = conn.execute(
                        """INSERT OR IGNORE INTO browser_wake_pending_worker
                           (message_id,route_key,destination,payload) VALUES(?,?,?,?)""",
                        (message_id, key, destination, payload),
                    )
                    results.append({
                        **worker,
                        "outcome": "queued",
                        "deduplicated": cur.rowcount == 0,
                    })
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        except sqlite3.Error as exc:
            raise WakeAllError("canonical_wake_enqueue_failed") from exc
        finally:
            conn.close()

        aggregate = {
            key: sum(1 for x in results if x.get("outcome") == key)
            for key in ("queued", "verified", "blocked", "failed-pre-send", "uncertain", "skipped")
        }
        aggregate["skipped"] += len(skipped)
        return sanitize(
            {
                "idempotent_replay": is_replay,
                "counts": aggregate,
                "queued_count": aggregate["queued"],
                "skipped_count": aggregate["skipped"],
                "results": results,
                "skipped": skipped,
            }
        )


    def result(self, *, idempotency_key: str) -> dict[str, Any]:
        if not self.enabled:
            raise WakeAllError("capability_disabled")
        if not re.fullmatch(r"[A-Za-z0-9_-]{20,120}", idempotency_key or ""):
            raise WakeAllError("invalid_idempotency_key")
        batch_prefix = "control-center-wake:" + hashlib.sha256(idempotency_key.encode()).hexdigest()[:20] + ":"
        conn = _connect(self.wake_db, writable=False)
        try:
            pending = {
                str(row["message_id"]): str(row["route_key"])
                for row in conn.execute(
                    "SELECT message_id,route_key FROM browser_wake_pending_worker WHERE message_id LIKE ?",
                    (batch_prefix + "%",),
                ).fetchall()
            }
            deliveries = {
                str(row["message_id"]): {
                    "worker_key": str(row["route_key"]),
                    "status": str(row["status"] or "").upper(),
                    "attempts": int(row["attempts"] or 0),
                }
                for row in conn.execute(
                    """SELECT message_id,route_key,status,attempts
                         FROM browser_wake_delivery WHERE message_id LIKE ?""",
                    (batch_prefix + "%",),
                ).fetchall()
            }
        finally:
            conn.close()

        message_ids = sorted(set(pending) | set(deliveries))
        items: list[dict[str, Any]] = []
        for message_id in message_ids:
            delivery = deliveries.get(message_id)
            worker_key = delivery["worker_key"] if delivery else pending[message_id]
            status = delivery["status"] if delivery else "PENDING"
            if status == "DELIVERED":
                outcome = "verified"
            elif status == "UNCERTAIN":
                outcome = "uncertain"
            elif status == "BLOCKED":
                outcome = "blocked"
            elif status == "FAILED_PRE_SEND":
                outcome = "failed-pre-send"
            else:
                outcome = "queued"
            items.append({
                "worker_key": sanitize(worker_key),
                "outcome": outcome,
                "delivery_status": status,
                "attempts": delivery["attempts"] if delivery else 0,
            })
        counts = {
            key: sum(1 for item in items if item["outcome"] == key)
            for key in ("queued", "verified", "blocked", "failed-pre-send", "uncertain")
        }
        return sanitize({
            "results": items,
            "counts": counts,
            "complete": not any(item["outcome"] == "queued" for item in items),
        })
