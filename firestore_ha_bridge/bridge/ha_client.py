from __future__ import annotations

from typing import Any

import requests


class HomeAssistantClient:
    def __init__(self, base_url: str, access_token: str, timeout_seconds: float = 8.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.access_token = access_token.strip()
        self.timeout_seconds = timeout_seconds

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }

    def ping(self) -> None:
        response = requests.get(
            f"{self.base_url}/api/",
            headers=self._headers(),
            timeout=self.timeout_seconds,
        )
        if not response.ok:
            raise RuntimeError(f"Home Assistant ping failed ({response.status_code}): {response.text}")

    def light_set(self, entity_id: str, brightness_pct: float | int | None = None) -> None:
        body: dict[str, Any] = {"entity_id": entity_id}
        if brightness_pct is not None:
            pct = max(1, min(100, int(round(float(brightness_pct)))))
            body["brightness_pct"] = pct
        response = requests.post(
            f"{self.base_url}/api/services/light/turn_on",
            headers=self._headers(),
            json=body,
            timeout=self.timeout_seconds,
        )
        if not response.ok:
            raise RuntimeError(f"Home Assistant light/turn_on failed ({response.status_code}): {response.text}")

    def light_off(self, entity_id: str) -> None:
        response = requests.post(
            f"{self.base_url}/api/services/light/turn_off",
            headers=self._headers(),
            json={"entity_id": entity_id},
            timeout=self.timeout_seconds,
        )
        if not response.ok:
            raise RuntimeError(f"Home Assistant light/turn_off failed ({response.status_code}): {response.text}")

    def scene_activate(self, entity_id: str, transition: float | int | None = None) -> None:
        body: dict[str, Any] = {"entity_id": entity_id}
        if transition is not None:
            body["transition"] = float(transition)
        response = requests.post(
            f"{self.base_url}/api/services/scene/turn_on",
            headers=self._headers(),
            json=body,
            timeout=self.timeout_seconds,
        )
        if not response.ok:
            raise RuntimeError(f"Home Assistant scene/turn_on failed ({response.status_code}): {response.text}")

    def fetch_light_states(self, entity_ids: list[str]) -> list[dict[str, Any]]:
        response = requests.get(
            f"{self.base_url}/api/states",
            headers=self._headers(),
            timeout=self.timeout_seconds,
        )
        if not response.ok:
            raise RuntimeError(f"Home Assistant states failed ({response.status_code}): {response.text}")

        payload = response.json()
        by_id = {
            str(entry.get("entity_id", "")).strip().lower(): entry
            for entry in payload
            if isinstance(entry, dict)
        }

        lights: list[dict[str, Any]] = []
        for entity_id in entity_ids:
            normalized = entity_id.strip().lower()
            state = by_id.get(normalized)
            if not state:
                continue
            attributes = state.get("attributes") if isinstance(state.get("attributes"), dict) else {}
            brightness_pct: int | None = None
            raw_brightness = attributes.get("brightness")
            if isinstance(raw_brightness, (int, float)) and raw_brightness >= 0:
                # HA brightness is 0–255.
                brightness_pct = max(1, min(100, int(round((float(raw_brightness) / 255.0) * 100))))
            entry: dict[str, Any] = {
                "entity_id": normalized,
                "state": str(state.get("state", "unknown")),
                "attributes": attributes,
            }
            if brightness_pct is not None and str(entry["state"]).lower() == "on":
                entry["brightnessPct"] = brightness_pct
            lights.append(entry)
        return lights
