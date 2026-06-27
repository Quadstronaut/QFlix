"""Listmonk campaign API client — create + fire-and-archive."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import requests

from .config import Config

logger = logging.getLogger(__name__)
DEFAULT_TIMEOUT_S = 30


@dataclass
class CampaignResult:
    campaign_id: int
    status: str
    archive_url: Optional[str]


def create_and_send_campaign(
    cfg: Config,
    *,
    subject: str,
    html_body: str,
    name: Optional[str] = None,
    list_ids: Optional[list[int]] = None,
    archive: bool = True,
    dry_run: bool = False,
) -> CampaignResult:
    """Create a Listmonk campaign with the given HTML body, then start it.

    Returns the campaign id + final status. Archive flag defaults to True so the
    "View in browser" link in delivered emails is reachable.
    """
    list_ids = list_ids or [cfg.listmonk_list_id]
    name = name or subject

    auth = (cfg.listmonk_api_user, cfg.listmonk_api_token)

    payload = {
        "name": name,
        "subject": subject,
        "lists": list_ids,
        "from_email": "",  # use Listmonk's configured default
        "type": "regular",
        "content_type": "html",
        "body": html_body,
        "archive": archive,
        "archive_template_id": cfg.listmonk_template_id,
    }
    if dry_run:
        logger.info("dry-run: would POST campaign name=%r subject=%r body_bytes=%d", name, subject, len(html_body))
        return CampaignResult(campaign_id=-1, status="dry-run", archive_url=None)

    r = requests.post(
        f"{cfg.listmonk_base_url}/api/campaigns",
        json=payload,
        auth=auth,
        timeout=DEFAULT_TIMEOUT_S,
    )
    # Log the response body before re-raising — a 4xx (duplicate campaign name,
    # bad list id) or 5xx used to vanish into the bare HTTPError with no clue
    # what Listmonk actually complained about. The systemd journal entry is
    # the operator's only signal when this happens during the Monday 08:00
    # run, so make it speak.
    if r.status_code >= 400:
        logger.error(
            "listmonk POST /api/campaigns failed: HTTP %d — body=%s",
            r.status_code, (r.text or "")[:500],
        )
    r.raise_for_status()
    body = r.json().get("data") or {}
    campaign_id = int(body.get("id") or 0)
    if not campaign_id:
        raise RuntimeError(f"listmonk campaign create returned no id: {body}")

    status_resp = requests.put(
        f"{cfg.listmonk_base_url}/api/campaigns/{campaign_id}/status",
        json={"status": "running"},
        auth=auth,
        timeout=DEFAULT_TIMEOUT_S,
    )
    if status_resp.status_code >= 400:
        logger.error(
            "listmonk PUT /api/campaigns/%d/status failed: HTTP %d — body=%s",
            campaign_id, status_resp.status_code,
            (status_resp.text or "")[:500],
        )
    status_resp.raise_for_status()

    archive_url = (
        f"https://{cfg.public_host}/listmonk/campaign/{body.get('uuid')}"
        if body.get("uuid") and archive
        else None
    )
    return CampaignResult(campaign_id=campaign_id, status="running", archive_url=archive_url)


def send_test_campaign(
    cfg: Config,
    *,
    subject: str,
    html_body: str,
    to_emails: list[str],
    name: Optional[str] = None,
) -> CampaignResult:
    """Send a single true-to-production render to specific addresses only.

    Creates a *draft* campaign with the real subject/body, then fires Listmonk's
    test endpoint (`POST /campaigns/{id}/test`) at the given addresses. The
    campaign stays a draft — the subscriber list is never mailed. Recipients
    must already exist as Listmonk subscribers (Listmonk looks them up); a
    non-subscriber yields a 4xx we surface in the log.
    """
    auth = (cfg.listmonk_api_user, cfg.listmonk_api_token)
    name = name or f"[TEST] {subject}"

    create_payload = {
        "name": name,
        "subject": subject,
        "lists": [cfg.listmonk_list_id],
        "from_email": "",
        "type": "regular",
        "content_type": "html",
        "body": html_body,
    }
    r = requests.post(
        f"{cfg.listmonk_base_url}/api/campaigns",
        json=create_payload,
        auth=auth,
        timeout=DEFAULT_TIMEOUT_S,
    )
    if r.status_code >= 400:
        logger.error(
            "listmonk POST /api/campaigns (test) failed: HTTP %d — body=%s",
            r.status_code, (r.text or "")[:500],
        )
    r.raise_for_status()
    body = r.json().get("data") or {}
    campaign_id = int(body.get("id") or 0)
    if not campaign_id:
        raise RuntimeError(f"listmonk test campaign create returned no id: {body}")

    test_payload = {
        "subscribers": to_emails,
        "lists": [cfg.listmonk_list_id],
        "messenger": "email",
        "name": name,
        "subject": subject,
        "from_email": "",
        "content_type": "html",
        "body": html_body,
        "template_id": body.get("template_id"),
    }
    tr = requests.post(
        f"{cfg.listmonk_base_url}/api/campaigns/{campaign_id}/test",
        json=test_payload,
        auth=auth,
        timeout=DEFAULT_TIMEOUT_S,
    )
    if tr.status_code >= 400:
        logger.error(
            "listmonk POST /api/campaigns/%d/test failed: HTTP %d — body=%s",
            campaign_id, tr.status_code, (tr.text or "")[:500],
        )
    tr.raise_for_status()
    return CampaignResult(campaign_id=campaign_id, status="test-sent", archive_url=None)
