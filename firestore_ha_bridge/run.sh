#!/usr/bin/with-contenv bashio
# shellcheck shell=bash

set -euo pipefail

export FIRESTORE_BRIDGE_DATA_DIR="/data"
export ADDON_VERSION="$(bashio::addon.version)"
export ORG_ID="$(bashio::config 'org_id')"
export SETUP_ID="$(bashio::config 'setup_id')"
export DEVICE_ID="$(bashio::config 'device_id')"
export EDGE_BASE_URL="$(bashio::config 'edge_base_url')"
export PAIRING_CODE="$(bashio::config 'pairing_code')"
export HA_BASE_URL="$(bashio::config 'ha_base_url')"
export HA_ACCESS_TOKEN="$(bashio::config 'ha_access_token')"
export FIREBASE_API_KEY="$(bashio::config 'firebase_api_key')"
export FIREBASE_PROJECT_ID="$(bashio::config 'firebase_project_id')"
export POLL_INTERVAL_SECONDS="$(bashio::config 'poll_interval_seconds')"
export STATE_INTERVAL_SECONDS="$(bashio::config 'state_interval_seconds')"
export HEARTBEAT_INTERVAL_SECONDS="$(bashio::config 'heartbeat_interval_seconds')"
export IDLE_PAUSE_HOURS="$(bashio::config 'idle_pause_hours')"

exec python3 /app/bridge/main.py
