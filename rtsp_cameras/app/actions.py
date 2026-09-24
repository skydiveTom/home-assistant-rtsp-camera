"""Small request/response channel between the add-on and the integration.

The add-on writes an action file into the folder both sides share
(``<config>/rtsp_cameras``); the integration watches it and performs the action
through Home Assistant, which has full access to Supervisor. That keeps the
update button working even when the add-on itself has no Supervisor token.
"""

from __future__ import annotations

import json
import logging
import tempfile
from pathlib import Path
from typing import Any

from .config import Settings
from .models import utcnow

_LOGGER = logging.getLogger(__name__)

ACTION_INSTALL_ADDON_UPDATE = "install_addon_update"
ACTION_REFRESH_ADDON_UPDATE = "refresh_addon_update"


def request_action(settings: Settings, action: str, **values: Any) -> bool:
    """Write an action request for the integration, returning False on failure."""
    path = settings.actions_file
    payload = {
        "action": action,
        "requested_at": utcnow(),
        "handled_at": None,
        **values,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="\n",
            dir=str(path.parent),
            prefix=f"{path.name}.",
            suffix=".tmp",
            delete=False,
        )
        try:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        finally:
            handle.close()
        Path(handle.name).replace(path)
    except OSError as err:
        _LOGGER.error("Cannot write the action file %s: %s", path, err)
        return False

    _LOGGER.info("Requested %s through Home Assistant (%s)", action, path)
    return True
