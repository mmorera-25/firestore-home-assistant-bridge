from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

import requests

LOG = logging.getLogger("firestore-ha-bridge.firebase")

AuthFailureCallback = Callable[[str], None]
WriteMeterCallback = Callable[[], None]
CredentialsRotatedCallback = Callable[[str], None]

# Only these mean stored credentials are dead and must be cleared / re-paired.
_HARD_AUTH_FAILURE_MARKERS = (
    "USER_DISABLED",
    "USER_NOT_FOUND",
    "INVALID_REFRESH_TOKEN",
    "TOKEN_EXPIRED",
)

# Refresh ID token before this many seconds of age (Firebase tokens last ~3600s).
_ID_TOKEN_REFRESH_AFTER_SECONDS = 50 * 60


def _convert_firestore_value(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    if "stringValue" in value:
        return value["stringValue"]
    if "integerValue" in value:
        return int(value["integerValue"])
    if "doubleValue" in value:
        return float(value["doubleValue"])
    if "booleanValue" in value:
        return value["booleanValue"] is True or value["booleanValue"] == "true"
    if "nullValue" in value:
        return None
    if "mapValue" in value:
        fields = value["mapValue"].get("fields", {})
        return {key: _convert_firestore_value(entry) for key, entry in fields.items()}
    if "arrayValue" in value:
        return [_convert_firestore_value(entry) for entry in value["arrayValue"].get("values", [])]
    return {key: _convert_firestore_value(entry) for key, entry in value.items()}


def flatten_firestore_fields(fields: dict[str, Any]) -> dict[str, Any]:
    return {key: _convert_firestore_value(value) for key, value in fields.items()}


def encode_firestore_value(value: Any) -> dict[str, Any]:
    if value is None:
        return {"nullValue": None}
    if isinstance(value, bool):
        return {"booleanValue": value}
    if isinstance(value, int):
        return {"integerValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    if isinstance(value, str):
        return {"stringValue": value}
    if isinstance(value, list):
        return {"arrayValue": {"values": [encode_firestore_value(entry) for entry in value]}}
    if isinstance(value, dict):
        return {
            "mapValue": {
                "fields": {
                    key: encode_firestore_value(entry)
                    for key, entry in value.items()
                },
            },
        }
    return {"stringValue": str(value)}


def encode_firestore_fields(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {key: encode_firestore_value(value) for key, value in payload.items()}


def parse_firestore_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class FirebaseSession:
    def __init__(
        self,
        api_key: str,
        project_id: str,
        *,
        on_auth_failure: AuthFailureCallback | None = None,
        on_write: WriteMeterCallback | None = None,
        on_credentials_rotated: CredentialsRotatedCallback | None = None,
    ) -> None:
        self.api_key = api_key
        self.project_id = project_id
        self.id_token = ""
        self.refresh_token = ""
        self._on_auth_failure = on_auth_failure
        self._on_write = on_write
        self._on_credentials_rotated = on_credentials_rotated
        self._known_documents: set[str] = set()
        self._lock = threading.Lock()
        self._id_token_acquired_at = 0.0

    def _mark_id_token_fresh(self) -> None:
        self._id_token_acquired_at = time.monotonic()

    def _notify_credentials_rotated(self) -> None:
        if self._on_credentials_rotated and self.refresh_token:
            try:
                self._on_credentials_rotated(self.refresh_token)
            except Exception as error:  # noqa: BLE001
                LOG.warning("Could not persist rotated Firebase credentials: %s", error)

    def sign_in_with_custom_token(self, custom_token: str) -> None:
        response = requests.post(
            f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithCustomToken?key={self.api_key}",
            json={"token": custom_token, "returnSecureToken": True},
            timeout=30,
        )
        if not response.ok:
            raise RuntimeError(f"Firebase custom token sign-in failed: {response.text}")
        payload = response.json()
        self.id_token = str(payload.get("idToken", ""))
        self.refresh_token = str(payload.get("refreshToken", ""))
        if not self.id_token or not self.refresh_token:
            raise RuntimeError("Firebase sign-in response missing tokens.")
        self._mark_id_token_fresh()
        self._notify_credentials_rotated()

    def refresh_with_token(self, refresh_token: str) -> None:
        response = requests.post(
            f"https://securetoken.googleapis.com/v1/token?key={self.api_key}",
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
            timeout=30,
        )
        if not response.ok:
            text = response.text
            if any(marker in text for marker in _HARD_AUTH_FAILURE_MARKERS):
                if self._on_auth_failure:
                    self._on_auth_failure(text)
            raise RuntimeError(f"Firebase token refresh failed: {text}")
        payload = response.json()
        self.id_token = str(payload.get("id_token", ""))
        new_refresh = str(payload.get("refresh_token", "")).strip()
        if new_refresh:
            self.refresh_token = new_refresh
        elif refresh_token:
            self.refresh_token = refresh_token
        if not self.id_token:
            raise RuntimeError("Firebase refresh response missing id_token.")
        self._mark_id_token_fresh()
        self._notify_credentials_rotated()

    def ensure_fresh_token(self, *, force: bool = False) -> None:
        """Refresh the ID token before REST calls so long-running poll mode stays authed."""
        if not self.refresh_token:
            return
        age = time.monotonic() - self._id_token_acquired_at
        if not force and self.id_token and age < _ID_TOKEN_REFRESH_AFTER_SECONDS:
            return
        self.refresh_with_token(self.refresh_token)

    def _auth_headers(self) -> dict[str, str]:
        self.ensure_fresh_token()
        if not self.id_token:
            raise RuntimeError("Firebase session is not authenticated.")
        return {"Authorization": f"Bearer {self.id_token}"}

    def _document_url(self, path: str) -> str:
        clean = path.strip("/")
        return (
            f"https://firestore.googleapis.com/v1/projects/{self.project_id}"
            f"/databases/(default)/documents/{clean}"
        )

    def _note_write(self) -> None:
        if self._on_write:
            self._on_write()

    def _is_hard_auth_failure(self, text: str) -> bool:
        return any(marker in text for marker in _HARD_AUTH_FAILURE_MARKERS)

    def _raise_for_auth(self, response: requests.Response, action: str) -> None:
        if response.status_code not in {401, 403}:
            return
        text = response.text
        # Never wipe credentials on transient UNAUTHENTICATED / rules denials —
        # those used to brick paired Pis until manual re-pair.
        if self._on_auth_failure and self._is_hard_auth_failure(text):
            self._on_auth_failure(text)
        raise RuntimeError(f"Firestore {action} failed ({response.status_code}): {text}")

    def _request(
        self,
        method: str,
        url: str,
        *,
        action: str,
        params: Any = None,
        data: str | None = None,
        allow_404: bool = False,
    ) -> requests.Response:
        headers = {**self._auth_headers(), "Content-Type": "application/json"}
        response = requests.request(
            method,
            url,
            headers=headers,
            params=params,
            data=data,
            timeout=30,
        )
        if allow_404 and response.status_code == 404:
            return response
        # One retry after forced refresh for expired ID tokens.
        if response.status_code in {401, 403} and "UNAUTHENTICATED" in response.text:
            try:
                self.ensure_fresh_token(force=True)
            except RuntimeError:
                self._raise_for_auth(response, action)
                raise
            headers = {**self._auth_headers(), "Content-Type": "application/json"}
            response = requests.request(
                method,
                url,
                headers=headers,
                params=params,
                data=data,
                timeout=30,
            )
            if allow_404 and response.status_code == 404:
                return response
        self._raise_for_auth(response, action)
        return response

    def get_document(self, path: str) -> dict[str, Any] | None:
        response = self._request("GET", self._document_url(path), action="get", allow_404=True)
        if response.status_code == 404:
            return None
        if not response.ok:
            raise RuntimeError(f"Firestore get failed ({response.status_code}): {response.text}")
        payload = response.json()
        fields = payload.get("fields", {})
        return flatten_firestore_fields(fields)

    def upsert_document(self, path: str, payload: dict[str, Any]) -> None:
        """PATCH-first when known (or after restart); create only on 404.

        Avoids create→409→patch write doubling after process restart when device/state
        docs already exist.
        """
        clean = path.strip("/")
        with self._lock:
            known = clean in self._known_documents

        if known:
            self.patch_document(path, payload)
            return

        # Prefer PATCH for long-lived docs (devices / state) that usually already exist.
        patch_response = self._request(
            "PATCH",
            self._document_url(path),
            action="patch",
            params=[("updateMask.fieldPaths", key) for key in payload.keys()],
            data=json.dumps({"fields": encode_firestore_fields(payload)}),
            allow_404=True,
        )
        if patch_response.status_code == 404:
            parent, doc_id = clean.rsplit("/", 1)
            create_url = (
                f"https://firestore.googleapis.com/v1/projects/{self.project_id}"
                f"/databases/(default)/documents/{parent}"
            )
            create_response = self._request(
                "POST",
                create_url,
                action="create",
                params={"documentId": doc_id},
                data=json.dumps({"fields": encode_firestore_fields(payload)}),
            )
            if create_response.status_code == 409:
                with self._lock:
                    self._known_documents.add(clean)
                self.patch_document(path, payload)
                return

            if not create_response.ok:
                raise RuntimeError(
                    f"Firestore upsert failed ({create_response.status_code}): {create_response.text}"
                )

            with self._lock:
                self._known_documents.add(clean)
            self._note_write()
            return

        if not patch_response.ok:
            raise RuntimeError(
                f"Firestore patch failed ({patch_response.status_code}): {patch_response.text}"
            )
        with self._lock:
            self._known_documents.add(clean)
        self._note_write()

    def patch_document(self, path: str, payload: dict[str, Any]) -> None:
        response = self._request(
            "PATCH",
            self._document_url(path),
            action="patch",
            params=[("updateMask.fieldPaths", key) for key in payload.keys()],
            data=json.dumps({"fields": encode_firestore_fields(payload)}),
        )
        if not response.ok:
            raise RuntimeError(f"Firestore patch failed ({response.status_code}): {response.text}")
        clean = path.strip("/")
        with self._lock:
            self._known_documents.add(clean)
        self._note_write()

    def delete_document(self, path: str) -> None:
        response = self._request(
            "DELETE",
            self._document_url(path),
            action="delete",
            allow_404=True,
        )
        if response.status_code == 404:
            return
        if not response.ok:
            raise RuntimeError(f"Firestore delete failed ({response.status_code}): {response.text}")
        with self._lock:
            self._known_documents.discard(path.strip("/"))
        self._note_write()

    def _run_commands_query(
        self,
        org_id: str,
        *,
        filters: list[dict[str, Any]],
        limit: int = 20,
        order_by: list[dict[str, Any]] | None = None,
    ) -> list[tuple[str, dict[str, Any]]]:
        parent = (
            f"projects/{self.project_id}/databases/(default)/documents/"
            f"organizations/{org_id}/integrations/homeAssistantBridge"
        )
        structured: dict[str, Any] = {
            "from": [{"collectionId": "commands"}],
            "where": {
                "compositeFilter": {
                    "op": "AND",
                    "filters": filters,
                },
            },
            "limit": limit,
        }
        if order_by:
            structured["orderBy"] = order_by

        response = self._request(
            "POST",
            f"https://firestore.googleapis.com/v1/{parent}:runQuery",
            action="query",
            data=json.dumps({"structuredQuery": structured}),
        )
        if not response.ok:
            raise RuntimeError(f"Firestore query failed ({response.status_code}): {response.text}")

        results: list[tuple[str, dict[str, Any]]] = []
        for row in response.json():
            document = row.get("document")
            if not document:
                continue
            name = str(document.get("name", ""))
            command_id = name.rsplit("/", 1)[-1]
            fields = flatten_firestore_fields(document.get("fields", {}))
            results.append((command_id, fields))
        return results

    def query_pending_commands(self, org_id: str, setup_id: str) -> list[tuple[str, dict[str, Any]]]:
        return self._run_commands_query(
            org_id,
            filters=[
                {
                    "fieldFilter": {
                        "field": {"fieldPath": "setupId"},
                        "op": "EQUAL",
                        "value": {"stringValue": setup_id},
                    },
                },
                {
                    "fieldFilter": {
                        "field": {"fieldPath": "status"},
                        "op": "EQUAL",
                        "value": {"stringValue": "pending"},
                    },
                },
            ],
            order_by=[{"field": {"fieldPath": "issuedAt"}, "direction": "ASCENDING"}],
        )

    def query_processing_commands(self, org_id: str, setup_id: str) -> list[tuple[str, dict[str, Any]]]:
        return self._run_commands_query(
            org_id,
            filters=[
                {
                    "fieldFilter": {
                        "field": {"fieldPath": "setupId"},
                        "op": "EQUAL",
                        "value": {"stringValue": setup_id},
                    },
                },
                {
                    "fieldFilter": {
                        "field": {"fieldPath": "status"},
                        "op": "EQUAL",
                        "value": {"stringValue": "processing"},
                    },
                },
            ],
        )

    def query_terminal_commands(
        self,
        org_id: str,
        setup_id: str,
        *,
        status: str,
        older_than: datetime,
        limit: int = 50,
    ) -> list[tuple[str, dict[str, Any]]]:
        cutoff = older_than.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        return self._run_commands_query(
            org_id,
            filters=[
                {
                    "fieldFilter": {
                        "field": {"fieldPath": "setupId"},
                        "op": "EQUAL",
                        "value": {"stringValue": setup_id},
                    },
                },
                {
                    "fieldFilter": {
                        "field": {"fieldPath": "status"},
                        "op": "EQUAL",
                        "value": {"stringValue": status},
                    },
                },
                {
                    "fieldFilter": {
                        "field": {"fieldPath": "processedAt"},
                        "op": "LESS_THAN",
                        "value": {"stringValue": cutoff},
                    },
                },
            ],
            limit=limit,
            # Inequality on processedAt requires orderBy on the same field (index exists).
            order_by=[{"field": {"fieldPath": "processedAt"}, "direction": "ASCENDING"}],
        )


class WriteRateLimiter:
    """Circuit breaker for Firestore writes."""

    def __init__(self, max_writes_per_minute: int = 60) -> None:
        self.max_writes_per_minute = max_writes_per_minute
        self._timestamps: list[float] = []
        self._lock = threading.Lock()
        self._open_until = 0.0
        self._backoff_seconds = 30.0

    def note_write(self) -> None:
        now = time.monotonic()
        with self._lock:
            self._timestamps.append(now)
            cutoff = now - 60.0
            self._timestamps = [stamp for stamp in self._timestamps if stamp >= cutoff]
            if len(self._timestamps) > self.max_writes_per_minute:
                self._open_until = now + self._backoff_seconds
                self._backoff_seconds = min(self._backoff_seconds * 2, 600.0)
                LOG.error(
                    "Write circuit breaker OPEN: %s writes in the last minute (limit %s). Pausing %.0fs.",
                    len(self._timestamps),
                    self.max_writes_per_minute,
                    self._open_until - now,
                )

    def allow(self) -> bool:
        now = time.monotonic()
        with self._lock:
            if now < self._open_until:
                return False
            if self._open_until and now >= self._open_until:
                self._open_until = 0.0
                self._backoff_seconds = 30.0
                LOG.warning("Write circuit breaker CLOSED; resuming Firestore writes.")
            return True

    def pause_remaining(self) -> float:
        with self._lock:
            return max(0.0, self._open_until - time.monotonic())
