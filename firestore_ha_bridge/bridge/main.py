#!/usr/bin/env python3
"""Firestore Home Assistant bridge — listens for Firestore commands and executes HA REST locally."""

from __future__ import annotations

import json
import logging
import os
import socket
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from firebase_client import FirebaseSession
from ha_client import HomeAssistantClient

LOG = logging.getLogger("firestore-ha-bridge")
VERSION = "0.1.0"
DATA_DIR = Path(os.environ.get("FIRESTORE_BRIDGE_DATA_DIR", "/data"))
CREDENTIALS_PATH = DATA_DIR / "bridge_credentials.json"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_config() -> dict[str, Any]:
    org_id = os.environ.get("ORG_ID", "").strip()
    setup_id = os.environ.get("SETUP_ID", "").strip()
    device_id = os.environ.get("DEVICE_ID", "").strip() or str(uuid.uuid4())
    edge_base_url = os.environ.get("EDGE_BASE_URL", "").strip().rstrip("/")
    pairing_code = os.environ.get("PAIRING_CODE", "").strip()
    ha_base_url = os.environ.get("HA_BASE_URL", "http://homeassistant:8123").strip().rstrip("/")
    ha_access_token = os.environ.get("HA_ACCESS_TOKEN", "").strip()
    firebase_api_key = os.environ.get("FIREBASE_API_KEY", "").strip()
    firebase_project_id = os.environ.get("FIREBASE_PROJECT_ID", "").strip()
    poll_interval = float(os.environ.get("POLL_INTERVAL_SECONDS", "2"))
    state_interval = float(os.environ.get("STATE_INTERVAL_SECONDS", "15"))
    heartbeat_interval = float(os.environ.get("HEARTBEAT_INTERVAL_SECONDS", "30"))

    return {
        "org_id": org_id,
        "setup_id": setup_id,
        "device_id": device_id,
        "edge_base_url": edge_base_url,
        "pairing_code": pairing_code,
        "ha_base_url": ha_base_url,
        "ha_access_token": ha_access_token,
        "firebase_api_key": firebase_api_key,
        "firebase_project_id": firebase_project_id,
        "poll_interval": poll_interval,
        "state_interval": state_interval,
        "heartbeat_interval": heartbeat_interval,
    }


def load_credentials() -> dict[str, str] | None:
    if not CREDENTIALS_PATH.exists():
        return None
    try:
        payload = json.loads(CREDENTIALS_PATH.read_text(encoding="utf-8"))
        refresh_token = str(payload.get("refresh_token", "")).strip()
        if refresh_token:
            return {"refresh_token": refresh_token}
    except (OSError, json.JSONDecodeError):
        return None
    return None


def save_credentials(refresh_token: str) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CREDENTIALS_PATH.write_text(
        json.dumps({"refresh_token": refresh_token}, indent=2),
        encoding="utf-8",
    )


def pair_device(config: dict[str, Any]) -> FirebaseSession:
    pairing_code = config["pairing_code"]
    if not pairing_code:
        raise RuntimeError("PAIRING_CODE is required for first-time pairing.")

    import requests

    response = requests.post(
        f"{config['edge_base_url']}/api/integrations/ha/bridge/pair",
        json={
            "orgId": config["org_id"],
            "setupId": config["setup_id"],
            "pairingCode": pairing_code,
            "deviceId": config["device_id"],
            "hostname": socket.gethostname(),
        },
        timeout=30,
    )
    if not response.ok:
        raise RuntimeError(f"Pairing failed ({response.status_code}): {response.text}")

    payload = response.json()
    custom_token = str(payload.get("customToken", "")).strip()
    if not custom_token:
        raise RuntimeError("Pairing response did not include customToken.")

    session = FirebaseSession(
        api_key=config["firebase_api_key"],
        project_id=config["firebase_project_id"],
    )
    session.sign_in_with_custom_token(custom_token)
    save_credentials(session.refresh_token)
    LOG.info("Paired bridge device %s for org %s", config["device_id"], config["org_id"])
    return session


def resolve_firebase_session(config: dict[str, Any]) -> FirebaseSession:
    session = FirebaseSession(
        api_key=config["firebase_api_key"],
        project_id=config["firebase_project_id"],
    )
    stored = load_credentials()
    if stored:
        session.refresh_with_token(stored["refresh_token"])
        return session
    return pair_device(config)


def extract_light_entity_ids(org_doc: dict[str, Any] | None, setup_id: str) -> list[str]:
    if not org_doc:
        return []
    settings = org_doc.get("settings")
    if not isinstance(settings, dict):
        return []
    integrations = settings.get("integrations")
    if not isinstance(integrations, dict):
        return []
    ha = integrations.get("homeAssistant")
    if not isinstance(ha, dict):
        return []
    setups_raw = ha.get("setups")
    if not isinstance(setups_raw, list):
        return []

    for setup in setups_raw:
        if not isinstance(setup, dict) or str(setup.get("id", "")) != setup_id:
            continue
        lights = setup.get("lights")
        if not isinstance(lights, list):
            return []
        entity_ids: list[str] = []
        for light in lights:
            if not isinstance(light, dict):
                continue
            entity_id = str(light.get("entityId", "")).strip().lower()
            if entity_id:
                entity_ids.append(entity_id)
        return entity_ids
    return []


class BridgeRuntime:
    def __init__(self, config: dict[str, Any], firebase: FirebaseSession, ha: HomeAssistantClient) -> None:
        self.config = config
        self.firebase = firebase
        self.ha = ha
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def heartbeat_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.firebase.upsert_document(
                    f"organizations/{self.config['org_id']}/integrations/homeAssistantBridge/devices/{self.config['device_id']}",
                    {
                        "id": self.config["device_id"],
                        "organizationId": self.config["org_id"],
                        "setupId": self.config["setup_id"],
                        "lastSeen": utc_now_iso(),
                        "status": "online",
                        "version": VERSION,
                        "hostname": socket.gethostname(),
                    },
                )
            except Exception as error:  # noqa: BLE001
                LOG.warning("Heartbeat failed: %s", error)
            self._stop.wait(self.config["heartbeat_interval"])

    def state_loop(self) -> None:
        while not self._stop.is_set():
            try:
                org_doc = self.firebase.get_document(f"organizations/{self.config['org_id']}")
                entity_ids = extract_light_entity_ids(org_doc, self.config["setup_id"])
                if entity_ids:
                    lights = self.ha.fetch_light_states(entity_ids)
                    self.firebase.upsert_document(
                        f"organizations/{self.config['org_id']}/integrations/homeAssistantBridge/state/{self.config['setup_id']}",
                        {
                            "setupId": self.config["setup_id"],
                            "organizationId": self.config["org_id"],
                            "updatedAt": utc_now_iso(),
                            "bridgeDeviceId": self.config["device_id"],
                            "lights": lights,
                        },
                    )
            except Exception as error:  # noqa: BLE001
                LOG.warning("State publish failed: %s", error)
            self._stop.wait(self.config["state_interval"])

    def process_command(self, command_id: str, command: dict[str, Any]) -> None:
        base_path = (
            f"organizations/{self.config['org_id']}/integrations/homeAssistantBridge/commands/{command_id}"
        )
        now = utc_now_iso()
        self.firebase.patch_document(base_path, {
            "status": "processing",
            "processingStartedAt": now,
        })

        try:
            command_type = str(command.get("type", ""))
            if command_type == "ping":
                self.ha.ping()
            elif command_type == "lightSet":
                entity_id = str(command.get("entityId", ""))
                brightness = command.get("brightnessPct")
                self.ha.light_set(entity_id, brightness_pct=brightness if isinstance(brightness, (int, float)) else None)
            elif command_type == "lightOff":
                self.ha.light_off(str(command.get("entityId", "")))
            elif command_type == "sceneActivate":
                transition = command.get("transition")
                self.ha.scene_activate(
                    str(command.get("entityId", "")),
                    transition=transition if isinstance(transition, (int, float)) else None,
                )
            else:
                raise RuntimeError(f"Unsupported command type: {command_type}")

            self.firebase.patch_document(base_path, {
                "status": "applied",
                "processedAt": utc_now_iso(),
            })
        except Exception as error:  # noqa: BLE001
            LOG.exception("Command %s failed", command_id)
            self.firebase.patch_document(base_path, {
                "status": "failed",
                "processedAt": utc_now_iso(),
                "errorMessage": str(error),
            })

    def command_loop(self) -> None:
        while not self._stop.is_set():
            try:
                pending = self.firebase.query_pending_commands(
                    self.config["org_id"],
                    self.config["setup_id"],
                )
                for command_id, command in pending:
                    if self._stop.is_set():
                        break
                    self.process_command(command_id, command)
            except Exception as error:  # noqa: BLE001
                LOG.warning("Command poll failed: %s", error)
            self._stop.wait(self.config["poll_interval"])

    def run(self) -> None:
        LOG.info(
            "Starting Firestore HA bridge v%s (org=%s setup=%s device=%s)",
            VERSION,
            self.config["org_id"],
            self.config["setup_id"],
            self.config["device_id"],
        )
        self.ha.ping()

        threads = [
            threading.Thread(target=self.heartbeat_loop, name="heartbeat", daemon=True),
            threading.Thread(target=self.state_loop, name="state", daemon=True),
            threading.Thread(target=self.command_loop, name="commands", daemon=True),
        ]
        for thread in threads:
            thread.start()

        try:
            while not self._stop.is_set():
                time.sleep(1)
        except KeyboardInterrupt:
            self.stop()


def validate_config(config: dict[str, Any]) -> None:
    missing = [
        key for key in (
            "org_id",
            "setup_id",
            "edge_base_url",
            "ha_access_token",
            "firebase_api_key",
            "firebase_project_id",
        )
        if not config[key]
    ]
    if missing:
        raise RuntimeError(f"Missing required configuration: {', '.join(missing)}")
    if not load_credentials() and not config["pairing_code"]:
        raise RuntimeError("PAIRING_CODE is required until bridge credentials are stored.")


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    config = read_config()
    validate_config(config)
    firebase = resolve_firebase_session(config)
    ha = HomeAssistantClient(config["ha_base_url"], config["ha_access_token"])
    runtime = BridgeRuntime(config, firebase, ha)
    runtime.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
