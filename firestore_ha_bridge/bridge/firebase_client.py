from __future__ import annotations

import json
from typing import Any

import requests


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


class FirebaseSession:
    def __init__(self, api_key: str, project_id: str) -> None:
        self.api_key = api_key
        self.project_id = project_id
        self.id_token = ""
        self.refresh_token = ""

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
            raise RuntimeError(f"Firebase token refresh failed: {response.text}")
        payload = response.json()
        self.id_token = str(payload.get("id_token", ""))
        self.refresh_token = str(payload.get("refresh_token", refresh_token))
        if not self.id_token:
            raise RuntimeError("Firebase refresh response missing id_token.")

    def _auth_headers(self) -> dict[str, str]:
        if not self.id_token:
            raise RuntimeError("Firebase session is not authenticated.")
        return {"Authorization": f"Bearer {self.id_token}"}

    def _document_url(self, path: str) -> str:
        clean = path.strip("/")
        return (
            f"https://firestore.googleapis.com/v1/projects/{self.project_id}"
            f"/databases/(default)/documents/{clean}"
        )

    def get_document(self, path: str) -> dict[str, Any] | None:
        response = requests.get(self._document_url(path), headers=self._auth_headers(), timeout=30)
        if response.status_code == 404:
            return None
        if not response.ok:
            raise RuntimeError(f"Firestore get failed ({response.status_code}): {response.text}")
        payload = response.json()
        fields = payload.get("fields", {})
        return flatten_firestore_fields(fields)

    def upsert_document(self, path: str, payload: dict[str, Any]) -> None:
        clean = path.strip("/")
        parent, doc_id = clean.rsplit("/", 1)
        create_url = (
            f"https://firestore.googleapis.com/v1/projects/{self.project_id}"
            f"/databases/(default)/documents/{parent}"
        )
        response = requests.post(
            create_url,
            headers={**self._auth_headers(), "Content-Type": "application/json"},
            params={"documentId": doc_id},
            data=json.dumps({"fields": encode_firestore_fields(payload)}),
            timeout=30,
        )
        if response.status_code == 409:
            self.patch_document(path, payload)
            return
        if not response.ok:
            raise RuntimeError(f"Firestore upsert failed ({response.status_code}): {response.text}")

    def patch_document(self, path: str, payload: dict[str, Any]) -> None:
        response = requests.patch(
            self._document_url(path),
            headers={**self._auth_headers(), "Content-Type": "application/json"},
            params=[("updateMask.fieldPaths", key) for key in payload.keys()],
            data=json.dumps({"fields": encode_firestore_fields(payload)}),
            timeout=30,
        )
        if not response.ok:
            raise RuntimeError(f"Firestore patch failed ({response.status_code}): {response.text}")

    def query_pending_commands(self, org_id: str, setup_id: str) -> list[tuple[str, dict[str, Any]]]:
        parent = (
            f"projects/{self.project_id}/databases/(default)/documents/"
            f"organizations/{org_id}/integrations/homeAssistantBridge"
        )
        body = {
            "structuredQuery": {
                "from": [{"collectionId": "commands"}],
                "where": {
                    "compositeFilter": {
                        "op": "AND",
                        "filters": [
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
                    },
                },
                "orderBy": [{"field": {"fieldPath": "issuedAt"}, "direction": "ASCENDING"}],
                "limit": 20,
            },
        }
        response = requests.post(
            f"https://firestore.googleapis.com/v1/{parent}:runQuery",
            headers={**self._auth_headers(), "Content-Type": "application/json"},
            data=json.dumps(body),
            timeout=30,
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
