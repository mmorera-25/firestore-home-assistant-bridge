from __future__ import annotations

import logging
import os
import sys
import time
from typing import Any

import requests

LOG = logging.getLogger("firestore-ha-bridge")


def _supervisor_base_url() -> str:
    host = os.environ.get("SUPERVISOR", "supervisor").strip() or "supervisor"
    return f"http://{host}"


def _supervisor_headers() -> dict[str, str] | None:
    token = os.environ.get("SUPERVISOR_TOKEN", "").strip()
    if not token:
        return None
    return {"Authorization": f"Bearer {token}"}


def _read_addon_options(headers: dict[str, str]) -> dict[str, Any] | None:
    response = requests.get(
        f"{_supervisor_base_url()}/addons/self/info",
        headers=headers,
        timeout=15,
    )
    if not response.ok:
        LOG.warning("Could not read add-on options from Supervisor (%s): %s", response.status_code, response.text)
        return None

    payload = response.json()
    options = payload.get("data", {}).get("options")
    if not isinstance(options, dict):
        LOG.warning("Supervisor add-on info did not include options.")
        return None
    return dict(options)


def clear_pairing_code_option() -> bool:
    """Remove pairing_code from persisted add-on options after successful pairing."""
    headers = _supervisor_headers()
    if not headers:
        LOG.warning(
            "SUPERVISOR_TOKEN unavailable — clear pairing_code manually in add-on Configuration after pairing.",
        )
        return False

    options = _read_addon_options(headers)
    if options is None:
        return False

    if not str(options.get("pairing_code", "")).strip():
        return True

    options["pairing_code"] = ""
    response = requests.post(
        f"{_supervisor_base_url()}/addons/self/options",
        headers={**headers, "Content-Type": "application/json"},
        json={"options": options},
        timeout=15,
    )
    if not response.ok:
        LOG.warning("Could not clear pairing_code in add-on options (%s): %s", response.status_code, response.text)
        return False

    LOG.info("Cleared pairing_code from add-on configuration.")
    return True


def request_addon_restart() -> None:
    headers = _supervisor_headers()
    if not headers:
        return

    response = requests.post(
        f"{_supervisor_base_url()}/addons/self/restart",
        headers=headers,
        timeout=15,
    )
    if not response.ok:
        LOG.warning("Could not restart add-on after pairing (%s): %s", response.status_code, response.text)
        return

    LOG.info("Restarting add-on so Configuration reflects cleared pairing_code.")
    time.sleep(2)
    sys.exit(0)
