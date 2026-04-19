"""Truth Social polling + flag-percent / countdown math.

The flag rises in 10% increments every 30 minutes after the last post,
capping at 100% (= 5h30m of silence). The countdown exposed to the 7-seg
display is the number of seconds until the *next* 10% step.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from dateutil import parser as date_parse

log = logging.getLogger(__name__)

HALF_HOUR = 1800  # seconds


@dataclass
class Snapshot:
    """One polling result the service needs to act on."""

    id: str
    body_html: str
    created_at: datetime
    percent: int
    countdown_seconds: int   # -1 when flag is fully raised
    # First usable media attachment, if any. ``media_kind`` is "image"
    # (``url`` is the full-res image) or "video" (``url`` is the still
    # ``preview_url`` thumbnail; we never download the mp4).
    media_url: str | None = None
    media_kind: str | None = None


def compute(now: datetime, post_time: datetime) -> tuple[int, int]:
    """Return ``(percent, countdown_seconds)`` for a given post timestamp."""
    age = (now - post_time).total_seconds()
    if age < 0:
        age = 0
    if age < HALF_HOUR:
        pct = 0
    else:
        pct = min(int(age // HALF_HOUR) * 10, 100)
    countdown = -1 if pct >= 100 else int(HALF_HOUR - (age % HALF_HOUR))
    return pct, countdown


class TruthPoller:
    """Lazy wrapper around :mod:`truthbrush` that yields :class:`Snapshot`."""

    def __init__(self, handle: str):
        self._handle = handle
        self._api = None  # created on first use

    def _api_client(self):
        if self._api is None:
            from truthbrush.api import Api
            self._api = Api()
        return self._api

    def fetch(self) -> Optional[Snapshot]:
        api = self._api_client()
        latest = None
        for status in api.pull_statuses(self._handle):
            latest = status
            break
        if latest is None:
            log.warning("truth: no statuses returned for @%s", self._handle)
            return None

        post_time = date_parse.parse(latest["created_at"])
        if post_time.tzinfo is None:
            post_time = post_time.replace(tzinfo=timezone.utc)

        now = datetime.now(timezone.utc)
        pct, countdown = compute(now, post_time)
        body = latest.get("content", "") or ""

        # First usable media attachment becomes the OpenAI vision input
        # when the body is empty. Images use their full-res ``url``;
        # videos/gifvs fall back to the still ``preview_url`` thumbnail.
        media_url: str | None = None
        media_kind: str | None = None
        for att in latest.get("media_attachments") or []:
            att_type = att.get("type")
            if att_type == "image" and att.get("url"):
                media_url = att["url"]
                media_kind = "image"
                break
            if att_type in ("video", "gifv") and att.get("preview_url"):
                media_url = att["preview_url"]
                media_kind = "video"
                break

        log.info(
            "truth: @%s last post %.0fm ago → flag=%d%%, next-step in %ds%s",
            self._handle,
            (now - post_time).total_seconds() / 60,
            pct,
            countdown,
            f" (media={media_kind})" if media_kind else "",
        )
        return Snapshot(
            id=str(latest["id"]),
            body_html=body,
            created_at=post_time,
            percent=pct,
            countdown_seconds=countdown,
            media_url=media_url,
            media_kind=media_kind,
        )
