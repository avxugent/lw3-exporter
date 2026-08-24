"""
OpenBao credential lookup.

LW3's WebSocket management endpoint authenticates with HTTP Basic auth
(username + password), so this looks up both. Mirrors the pearl-exporter /
bao.py pattern for consistency across the fleet's exporters.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import hvac

logger = logging.getLogger(__name__)

# OpenBao is API-compatible with Vault, so we accept both BAO_* and VAULT_*
# environment variables. BAO_* takes precedence; VAULT_* is the fallback so
# Nomad's native Vault/OpenBao integration vars work out of the box.
BAO_ADDR = os.environ.get("BAO_ADDR") or os.environ.get("VAULT_ADDR")
BAO_TOKEN = os.environ.get("BAO_TOKEN") or os.environ.get("VAULT_TOKEN")
BAO_KV_MOUNT = os.environ.get("BAO_KV_MOUNT", "secret")
# KV secrets engine version: 2 (default) reads at <mount>/data/<path>; 1 reads at
# <mount>/<path> with no 'data' segment and a flat response shape.
BAO_KV_VERSION = os.environ.get("BAO_KV_VERSION", "2")
BAO_PATH_PREFIX = os.environ.get("BAO_PATH_PREFIX", "")
BAO_CACERT = os.environ.get("BAO_CACERT") or os.environ.get("VAULT_CACERT")
# Namespace (OpenBao/Vault Enterprise). hvac does not read this from the env, so
# it must be passed to the client explicitly. None => root namespace.
BAO_NAMESPACE = os.environ.get("BAO_NAMESPACE") or os.environ.get("VAULT_NAMESPACE")


def get_credentials(hostname: str) -> Optional[tuple[str, str]]:
    """Look up the LW3 (username, password) pair for a device hostname in OpenBao KV.

    Returns a (username, password) tuple, or None if the lookup fails for any
    reason (missing config/token, secret not found, network error, secret
    missing a 'username' or 'password' key). Secret values are never logged.
    """
    if not hostname:
        return None

    if not BAO_ADDR or not BAO_TOKEN:
        logger.warning("OpenBao address or token not configured; skipping lookup")
        return None

    # Common path with the hostname as the final segment. strip("/") keeps the
    # path clean when BAO_PATH_PREFIX is empty.
    secret_path = f"{BAO_PATH_PREFIX}/{hostname}".strip("/")

    try:
        # Construct per call so a token re-rendered into the env by Nomad is
        # picked up without restarting the process.
        client = hvac.Client(
            url=BAO_ADDR,
            token=BAO_TOKEN,
            namespace=BAO_NAMESPACE,
            verify=BAO_CACERT if BAO_CACERT else True,
        )
        if BAO_KV_VERSION == "1":
            resp = client.secrets.kv.v1.read_secret(
                mount_point=BAO_KV_MOUNT,
                path=secret_path,
            )
            data = resp["data"]
        else:
            resp = client.secrets.kv.v2.read_secret_version(
                mount_point=BAO_KV_MOUNT,
                path=secret_path,
            )
            data = resp["data"]["data"]
        return data["username"], data["password"]
    except Exception as e:
        logger.warning(f"OpenBao credential lookup failed for '{secret_path}': {e}")
        return None
