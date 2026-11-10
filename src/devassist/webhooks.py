"""What every GitLab webhook receiver needs: secret check and retry de-duplication.

Shared by the DevAssist service (`server.py`) and the GitLab Duo → Teams bridge
(`duo_bridge.py`), so both behave the same way.
"""

from __future__ import annotations

import hmac
from collections import OrderedDict
from collections.abc import Mapping


def check_gitlab_secret(headers: Mapping[str, str], secret: str) -> tuple[int, str] | None:
    """None when the request may go on; otherwise (HTTP status, error message).

    Without a configured secret the endpoint stays closed (503), so a forgotten
    variable never leaves an open door.
    """
    if not secret:
        return 503, "webhooks are disabled: GITLAB_WEBHOOK_SECRET is not set"
    if not hmac.compare_digest(headers.get("X-Gitlab-Token", "").encode(), secret.encode()):
        return 401, "unauthorized"
    return None


def delivery_id(headers: Mapping[str, str]) -> str:
    return headers.get("Idempotency-Key") or headers.get("X-Gitlab-Event-UUID", "")


class RecentDeliveries:
    """GitLab retries deliveries, and a retry must not repeat the work."""

    def __init__(self, size: int = 2000) -> None:
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._size = size

    def is_duplicate(self, delivery: str) -> bool:
        if not delivery:
            return False
        if delivery in self._seen:
            return True
        self._seen[delivery] = None
        if len(self._seen) > self._size:
            self._seen.popitem(last=False)
        return False

    def forget(self, delivery: str) -> None:
        """The work failed, so GitLab's retry of this delivery must be allowed through."""
        self._seen.pop(delivery, None)
