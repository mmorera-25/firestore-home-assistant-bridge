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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from firebase_client import FirebaseSession, WriteRateLimiter, parse_firestore_timestamp
from ha_client import HomeAssistantClient
from supervisor_options import clear_pairing_code_option, persist_device_id_option, request_addon_restart

LOG = logging.getLogger("firestore-ha-bridge")
VERSION = os.environ.get("ADDON_VERSION", "0.1.10").strip() or "0.1.10"
DATA_DIR = Path(os.environ.get("FIRESTORE_BRIDGE_DATA_DIR", "/data"))
CREDENTIALS_PATH = DATA_DIR / "bridge_credentials.json"

DEFAULT_STATE_INTERVAL = 60.0
DEFAULT_HEARTBEAT_INTERVAL = 60.0
# Safety poll when listen is unavailable / as catch-up between snapshots.
DEFAULT_COMMAND_CATCHUP_INTERVAL = 10.0
MIN_STATE_INTERVAL = 30.0
MIN_HEARTBEAT_INTERVAL = 30.0
MIN_COMMAND_CATCHUP_INTERVAL = 5.0
PROCESSING_STALE_SECONDS = 120.0
COMMAND_TTL_SECONDS = 24 * 60 * 60
COMMAND_CLEANUP_INTERVAL = 30 * 60
MAX_WRITES_PER_MINUTE = 60


def normalize_optional_config(value: str) -> str:
    cleaned = value.strip()
    if not cleaned or cleaned.lower() == "null":
        return ""
    return cleaned


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def clamp_interval(value: float, *, minimum: float, default: float) -> float:
    if value <= 0 or value != value:  # noqa: PLR0124 — NaN check
        return default
    return max(minimum, value)


def parse_interval_env(name: str, default: float) -> float:
    """Parse bashio/env interval values; tolerate empty, null, and non-numeric."""
    raw = os.environ.get(name, "").strip()
    if not raw or raw.lower() == "null":
        return default
    try:
        return float(raw)
    except ValueError:
        LOG.warning("Invalid %s=%r; using default %.0fs", name, raw, default)
        return default


def read_config() -> dict[str, Any]:
    org_id = normalize_optional_config(os.environ.get("ORG_ID", ""))
    setup_id = normalize_optional_config(os.environ.get("SETUP_ID", ""))
    device_id = normalize_optional_config(os.environ.get("DEVICE_ID", ""))
    if not device_id:
        stored = load_credentials()
        if stored and stored.get("device_id"):
            device_id = stored["device_id"]
    if not device_id:
        device_id = str(uuid.uuid4())
    edge_base_url = normalize_optional_config(os.environ.get("EDGE_BASE_URL", "")).rstrip("/")
    pairing_code = normalize_optional_config(os.environ.get("PAIRING_CODE", ""))
    ha_base_url = os.environ.get("HA_BASE_URL", "http://homeassistant:8123").strip().rstrip("/")
    ha_access_token = os.environ.get("HA_ACCESS_TOKEN", "").strip()
    firebase_api_key = os.environ.get("FIREBASE_API_KEY", "").strip()
    firebase_project_id = os.environ.get("FIREBASE_PROJECT_ID", "").strip()

    # Legacy poll_interval_seconds now means command catch-up interval (listen is primary).
    raw_catchup = parse_interval_env("POLL_INTERVAL_SECONDS", DEFAULT_COMMAND_CATCHUP_INTERVAL)
    raw_state = parse_interval_env("STATE_INTERVAL_SECONDS", DEFAULT_STATE_INTERVAL)
    raw_heartbeat = parse_interval_env("HEARTBEAT_INTERVAL_SECONDS", DEFAULT_HEARTBEAT_INTERVAL)

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
        "command_catchup_interval": clamp_interval(
            raw_catchup,
            minimum=MIN_COMMAND_CATCHUP_INTERVAL,
            default=DEFAULT_COMMAND_CATCHUP_INTERVAL,
        ),
        "state_interval": clamp_interval(
            raw_state,
            minimum=MIN_STATE_INTERVAL,
            default=DEFAULT_STATE_INTERVAL,
        ),
        "heartbeat_interval": clamp_interval(
            raw_heartbeat,
            minimum=MIN_HEARTBEAT_INTERVAL,
            default=DEFAULT_HEARTBEAT_INTERVAL,
        ),
    }


def load_credentials() -> dict[str, str] | None:
    if not CREDENTIALS_PATH.exists():
        return None
    try:
        payload = json.loads(CREDENTIALS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    refresh_token = normalize_optional_config(str(payload.get("refresh_token", "")))
    if not refresh_token:
        return None
    stored: dict[str, str] = {"refresh_token": refresh_token}
    device_id = normalize_optional_config(str(payload.get("device_id", "")))
    if device_id:
        stored["device_id"] = device_id
    return stored


def clear_credentials() -> None:
    try:
        CREDENTIALS_PATH.unlink(missing_ok=True)
    except OSError as error:
        LOG.warning("Could not remove stored bridge credentials: %s", error)


def save_credentials(refresh_token: str, device_id: str) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CREDENTIALS_PATH.write_text(
        json.dumps({"refresh_token": refresh_token, "device_id": device_id}, indent=2),
        encoding="utf-8",
    )


def pair_device(config: dict[str, Any], firebase: FirebaseSession) -> FirebaseSession:
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

    firebase.sign_in_with_custom_token(custom_token)
    save_credentials(firebase.refresh_token, config["device_id"])
    LOG.info("Paired bridge device %s for org %s", config["device_id"], config["org_id"])
    persist_device_id_option(config["device_id"])
    if clear_pairing_code_option():
        request_addon_restart()
    return firebase


def resolve_firebase_session(config: dict[str, Any], firebase: FirebaseSession) -> FirebaseSession:
    stored = load_credentials()
    if stored:
        try:
            firebase.refresh_with_token(stored["refresh_token"])
            return firebase
        except RuntimeError as error:
            text = str(error)
            if not any(marker in text for marker in ("USER_NOT_FOUND", "USER_DISABLED", "INVALID_REFRESH_TOKEN")):
                raise
            LOG.warning(
                "Stored Firebase credentials are invalid (%s). "
                "Clearing saved credentials; set pairing_code and restart to pair again.",
                text,
            )
            clear_credentials()
            if not config["pairing_code"]:
                raise RuntimeError(
                    "Stored Firebase credentials are invalid. "
                    "Generate a new pairing code on desktop, set pairing_code, and restart."
                ) from error
            return pair_device(config, firebase)
    return pair_device(config, firebase)


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


def lights_signature(lights: list[dict[str, Any]]) -> str:
    normalized = sorted(
        (
            {
                "entityId": str(entry.get("entityId", "")),
                "state": str(entry.get("state", "")),
                "brightnessPct": entry.get("brightnessPct"),
            }
            for entry in lights
            if isinstance(entry, dict)
        ),
        key=lambda entry: entry["entityId"],
    )
    return json.dumps(normalized, sort_keys=True, separators=(",", ":"))


class BridgeRuntime:
    def __init__(self, config: dict[str, Any], firebase: FirebaseSession, ha: HomeAssistantClient) -> None:
        self.config = config
        self.firebase = firebase
        self.ha = ha
        self._stop = threading.Event()
        self._fatal = threading.Event()
        self._write_limiter = WriteRateLimiter(MAX_WRITES_PER_MINUTE)
        self._last_state_signature = ""
        self._command_lock = threading.Lock()
        self._in_flight_commands: set[str] = set()
        self._listen_watch = None

        self.firebase._on_write = self._write_limiter.note_write  # noqa: SLF001 — intentional wiring
        self.firebase._on_auth_failure = self._handle_auth_failure  # noqa: SLF001

    def _handle_auth_failure(self, detail: str) -> None:
        LOG.error("Firebase auth failure — stopping bridge: %s", detail)
        clear_credentials()
        self._fatal.set()
        self.stop()

    def stop(self) -> None:
        self._stop.set()
        watch = self._listen_watch
        if watch is not None:
            try:
                watch.unsubscribe()
            except Exception:  # noqa: BLE001
                pass
            self._listen_watch = None

    def _wait_if_circuit_open(self) -> bool:
        if self._write_limiter.allow():
            return True
        remaining = self._write_limiter.pause_remaining()
        self._stop.wait(min(max(remaining, 1.0), 60.0))
        return False

    def heartbeat_loop(self) -> None:
        while not self._stop.is_set():
            if not self._wait_if_circuit_open():
                continue
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
                if self._fatal.is_set():
                    return
            self._stop.wait(self.config["heartbeat_interval"])

    def publish_state_snapshot(self, *, force: bool = False) -> None:
        if not self._wait_if_circuit_open():
            return
        org_doc = self.firebase.get_document(f"organizations/{self.config['org_id']}")
        entity_ids = extract_light_entity_ids(org_doc, self.config["setup_id"])
        if not entity_ids:
            return

        lights = self.ha.fetch_light_states(entity_ids)
        signature = lights_signature(lights)
        if not force and signature == self._last_state_signature:
            return

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
        self._last_state_signature = signature

    def state_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.publish_state_snapshot(force=False)
            except Exception as error:  # noqa: BLE001
                LOG.warning("State publish failed: %s", error)
                if self._fatal.is_set():
                    return
            self._stop.wait(self.config["state_interval"])

    def process_command(self, command_id: str, command: dict[str, Any]) -> None:
        status = str(command.get("status", ""))
        if status in {"applied", "failed"}:
            return

        with self._command_lock:
            if command_id in self._in_flight_commands:
                return
            self._in_flight_commands.add(command_id)

        base_path = (
            f"organizations/{self.config['org_id']}/integrations/homeAssistantBridge/commands/{command_id}"
        )
        try:
            if not self._wait_if_circuit_open():
                return

            if status == "processing":
                started = parse_firestore_timestamp(command.get("processingStartedAt"))
                if started and datetime.now(timezone.utc) - started < timedelta(seconds=PROCESSING_STALE_SECONDS):
                    return
                LOG.warning("Reclaiming stale processing command %s", command_id)

            now = utc_now_iso()
            self.firebase.patch_document(base_path, {
                "status": "processing",
                "processingStartedAt": now,
            })

            try:
                command_type = str(command.get("type", ""))
                if command_type == "ping":
                    self.ha.ping()
                    LOG.info("Command %s applied (ping ok)", command_id)
                elif command_type == "lightSet":
                    entity_id = str(command.get("entityId", ""))
                    brightness = command.get("brightnessPct")
                    self.ha.light_set(
                        entity_id,
                        brightness_pct=brightness if isinstance(brightness, (int, float)) else None,
                    )
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
                if command_type in {"lightSet", "lightOff", "sceneActivate"}:
                    try:
                        self.publish_state_snapshot(force=True)
                    except Exception as error:  # noqa: BLE001
                        LOG.warning("Post-command state publish failed: %s", error)
            except Exception as error:  # noqa: BLE001
                LOG.exception("Command %s failed", command_id)
                self.firebase.patch_document(base_path, {
                    "status": "failed",
                    "processedAt": utc_now_iso(),
                    "errorMessage": str(error),
                })
        finally:
            with self._command_lock:
                self._in_flight_commands.discard(command_id)

    def _drain_pending_and_stale(self) -> int:
        pending = self.firebase.query_pending_commands(
            self.config["org_id"],
            self.config["setup_id"],
        )
        processing = self.firebase.query_processing_commands(
            self.config["org_id"],
            self.config["setup_id"],
        )
        todo = list(pending)
        for command_id, command in processing:
            todo.append((command_id, command))

        for command_id, command in todo:
            if self._stop.is_set():
                break
            LOG.info(
                "Processing command %s type=%s status=%s",
                command_id,
                command.get("type", ""),
                command.get("status", ""),
            )
            self.process_command(command_id, command)
        return len(todo)

    def _start_firestore_listen(self) -> bool:
        """Subscribe to pending commands via google-cloud-firestore on_snapshot when available."""
        try:
            from google.auth import credentials as ga_credentials
            from google.cloud import firestore
        except ImportError:
            LOG.warning(
                "google-cloud-firestore not installed; using catch-up poll only for commands "
                "(interval=%.0fs).",
                self.config["command_catchup_interval"],
            )
            return False

        class _FirebaseUserCredentials(ga_credentials.Credentials):
            def __init__(self, session: FirebaseSession) -> None:
                super().__init__()
                self._session = session
                self.token = session.id_token
                self.expiry = datetime.now(timezone.utc) + timedelta(minutes=50)

            def refresh(self, request):  # noqa: ANN001, ARG002
                self._session.refresh_with_token(self._session.refresh_token)
                self.token = self._session.id_token
                self.expiry = datetime.now(timezone.utc) + timedelta(minutes=50)

            @property
            def expired(self) -> bool:
                if not self.expiry:
                    return True
                return datetime.now(timezone.utc) >= self.expiry - timedelta(minutes=2)

        try:
            creds = _FirebaseUserCredentials(self.firebase)
            client = firestore.Client(project=self.config["firebase_project_id"], credentials=creds)
            query = (
                client.collection(
                    f"organizations/{self.config['org_id']}/integrations/homeAssistantBridge/commands"
                )
                .where("setupId", "==", self.config["setup_id"])
                .where("status", "==", "pending")
            )

            def _on_snapshot(col_snapshot, changes, read_time):  # noqa: ANN001, ARG001
                if self._stop.is_set() or self._fatal.is_set():
                    return
                for change in changes:
                    try:
                        doc = change.document
                        data = doc.to_dict() or {}
                        if str(data.get("status", "")) != "pending":
                            continue
                        LOG.info("Listen: pending command %s type=%s", doc.id, data.get("type", ""))
                        self.process_command(doc.id, data)
                    except Exception as error:  # noqa: BLE001
                        LOG.warning("Listen callback failed: %s", error)

            self._listen_watch = query.on_snapshot(_on_snapshot)
            LOG.info("Firestore listen attached for pending commands (setup=%s)", self.config["setup_id"])
            return True
        except Exception as error:  # noqa: BLE001
            LOG.warning("Could not start Firestore listen (%s); using catch-up poll.", error)
            self._listen_watch = None
            return False

    def command_catchup_loop(self) -> None:
        """Catch-up poll + reclaim stuck processing (complements listen push)."""
        while not self._stop.is_set():
            try:
                count = self._drain_pending_and_stale()
                if count:
                    LOG.info("Catch-up processed %s command(s)", count)
            except Exception as error:  # noqa: BLE001
                LOG.warning("Command catch-up failed: %s", error)
                if self._fatal.is_set():
                    return
            self._stop.wait(self.config["command_catchup_interval"])

    def cleanup_loop(self) -> None:
        while not self._stop.is_set():
            try:
                if self._wait_if_circuit_open():
                    cutoff = datetime.now(timezone.utc) - timedelta(seconds=COMMAND_TTL_SECONDS)
                    deleted = 0
                    for status in ("applied", "failed"):
                        stale = self.firebase.query_terminal_commands(
                            self.config["org_id"],
                            self.config["setup_id"],
                            status=status,
                            older_than=cutoff,
                        )
                        for command_id, _command in stale:
                            path = (
                                f"organizations/{self.config['org_id']}/integrations/"
                                f"homeAssistantBridge/commands/{command_id}"
                            )
                            self.firebase.delete_document(path)
                            deleted += 1
                    if deleted:
                        LOG.info("Cleaned up %s old command document(s)", deleted)
            except Exception as error:  # noqa: BLE001
                LOG.warning("Command cleanup failed: %s", error)
                if self._fatal.is_set():
                    return
            self._stop.wait(COMMAND_CLEANUP_INTERVAL)

    def run(self) -> None:
        LOG.info(
            "Starting Firestore HA bridge v%s (org=%s setup=%s device=%s) "
            "state=%ss heartbeat=%ss command_catchup=%ss",
            VERSION,
            self.config["org_id"],
            self.config["setup_id"],
            self.config["device_id"],
            int(self.config["state_interval"]),
            int(self.config["heartbeat_interval"]),
            int(self.config["command_catchup_interval"]),
        )
        self.ha.ping()
        self._start_firestore_listen()

        threads = [
            threading.Thread(target=self.heartbeat_loop, name="heartbeat", daemon=True),
            threading.Thread(target=self.state_loop, name="state", daemon=True),
            threading.Thread(target=self.command_catchup_loop, name="command-catchup", daemon=True),
            threading.Thread(target=self.cleanup_loop, name="cleanup", daemon=True),
        ]
        for thread in threads:
            thread.start()

        try:
            while not self._stop.is_set() and not self._fatal.is_set():
                time.sleep(1)
        except KeyboardInterrupt:
            self.stop()

        self.stop()
        if self._fatal.is_set():
            raise SystemExit(
                "Bridge stopped due to Firebase auth failure. "
                "Re-pair with a new pairing code when ready."
            )


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
    firebase = FirebaseSession(
        api_key=config["firebase_api_key"],
        project_id=config["firebase_project_id"],
    )
    firebase = resolve_firebase_session(config, firebase)
    ha = HomeAssistantClient(config["ha_base_url"], config["ha_access_token"])
    runtime = BridgeRuntime(config, firebase, ha)
    runtime.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
