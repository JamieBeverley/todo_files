"""Jira REST API v3 mapper."""
from __future__ import annotations

import logging

import requests
from requests.auth import HTTPBasicAuth

from ..models import FileConfig, Ticket
from .base import BaseMapper

_log = logging.getLogger(__name__)


class JiraMapper(BaseMapper):
    def __init__(self, base_url: str, username: str, api_token: str):
        self._base = base_url.rstrip("/")
        self._auth = HTTPBasicAuth(username, api_token)
        self._headers = {"Accept": "application/json", "Content-Type": "application/json"}
        self._active_sprint_cache: dict[str, int | None] = {}  # project_key → sprint id

    # ------------------------------------------------------------------
    # BaseMapper interface
    # ------------------------------------------------------------------

    def create(self, ticket: Ticket, config: FileConfig) -> str:
        payload = self._build_fields(ticket, config)
        if config.sprint:
            sprint_id = (
                self._resolve_active_sprint(config.board)
                if config.sprint == "current"
                else int(config.sprint)
            )
            if sprint_id is not None:
                payload["customfield_10020"] = sprint_id
        resp = self._post("/rest/api/3/issue", {"fields": payload})
        key = resp["key"]
        target_status = config.status_map.get(ticket.status)
        if target_status:
            self._transition(key, target_status)
        return key

    def update(self, ticket: Ticket, config: FileConfig) -> None:
        assert ticket.remote_key, "Cannot update a ticket without a remote key"
        payload = self._build_fields(ticket, config)
        self._put(f"/rest/api/3/issue/{ticket.remote_key}", {"fields": payload})

        # Status requires a separate transition call
        target_status = config.status_map.get(ticket.status)
        if target_status:
            self._transition(ticket.remote_key, target_status)

    def delete(self, remote_key: str) -> None:
        self._delete(f"/rest/api/3/issue/{remote_key}")

    def fetch(self, remote_key: str) -> Ticket:
        data = self._get(f"/rest/api/3/issue/{remote_key}")
        return self._issue_to_ticket(data)

    # ------------------------------------------------------------------
    # Field building
    # ------------------------------------------------------------------

    def _build_fields(self, ticket: Ticket, config: FileConfig) -> dict:
        item_type = ticket.item_type or config.item_type or "Task"
        fields: dict = {
            "summary": ticket.title,
            "issuetype": {"name": item_type.capitalize()},
            "project": {"key": config.board},
        }
        if ticket.labels or config.labels:
            fields["labels"] = ticket.labels or config.labels
        if config.assignee:
            fields["assignee"] = {"accountId": config.assignee}
        if ticket.description:
            fields["description"] = _text_to_adf(ticket.description)
        return fields

    # ------------------------------------------------------------------
    # Transitions
    # ------------------------------------------------------------------

    def _transition(self, remote_key: str, target_status_name: str) -> None:
        # Check current status first — skip the call if already there.
        current = self._get(f"/rest/api/3/issue/{remote_key}?fields=status")
        current_name = current["fields"]["status"]["name"]
        if current_name.lower() == target_status_name.lower():
            return

        transitions = self._get(f"/rest/api/3/issue/{remote_key}/transitions")
        match = next(
            (t for t in transitions["transitions"]
             if t["to"]["name"].lower() == target_status_name.lower()),
            None,
        )
        if match is None:
            raise ValueError(
                f"No transition to '{target_status_name}' found for {remote_key}. "
                f"Available: {[t['to']['name'] for t in transitions['transitions']]}"
            )
        self._post(
            f"/rest/api/3/issue/{remote_key}/transitions",
            {"transition": {"id": match["id"]}},
        )

    # ------------------------------------------------------------------
    # Issue → Ticket
    # ------------------------------------------------------------------

    @staticmethod
    def _issue_to_ticket(data: dict) -> Ticket:
        fields = data["fields"]
        description = _adf_to_text(fields.get("description")) if fields.get("description") else None
        jira_status = fields.get("status", {}).get("name", "")
        return Ticket(
            title=fields["summary"],
            status="",  # caller maps Jira status back to local code if desired
            remote_key=data["key"],
            item_type=fields.get("issuetype", {}).get("name"),
            description=description,
            labels=fields.get("labels", []),
            extra_fields={"_jira_status": jira_status} if jira_status else {},
        )

    # ------------------------------------------------------------------
    # Sprint helpers
    # ------------------------------------------------------------------

    def _resolve_active_sprint(self, project_key: str | None) -> int | None:
        """Return the active sprint ID for the project, with a per-instance cache."""
        if not project_key:
            return None
        if project_key in self._active_sprint_cache:
            return self._active_sprint_cache[project_key]

        boards = self._get(f"/rest/agile/1.0/board?projectKeyOrId={project_key}&type=scrum")
        if not boards.get("values"):
            _log.warning("No scrum board found for project %s", project_key)
            self._active_sprint_cache[project_key] = None
            return None

        board_id = boards["values"][0]["id"]
        sprints = self._get(f"/rest/agile/1.0/board/{board_id}/sprint?state=active")
        sprint_id = sprints["values"][0]["id"] if sprints.get("values") else None

        if sprint_id is None:
            _log.warning("No active sprint found for board %d", board_id)
        self._active_sprint_cache[project_key] = sprint_id
        return sprint_id

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _get(self, path: str) -> dict:
        url = self._base + path
        _log.debug("GET %s", url)
        resp = requests.get(url, auth=self._auth, headers=self._headers)
        _log.debug("GET %s → %s", url, resp.status_code)
        _raise(resp)
        return resp.json()

    def _post(self, path: str, body: dict) -> dict:
        url = self._base + path
        _log.debug("POST %s", url)
        resp = requests.post(url, json=body, auth=self._auth, headers=self._headers)
        _log.debug("POST %s → %s", url, resp.status_code)
        _raise(resp)
        return resp.json() if resp.content else {}

    def _put(self, path: str, body: dict) -> None:
        url = self._base + path
        _log.debug("PUT %s", url)
        resp = requests.put(url, json=body, auth=self._auth, headers=self._headers)
        _log.debug("PUT %s → %s", url, resp.status_code)
        _raise(resp)

    def _delete(self, path: str) -> None:
        url = self._base + path
        _log.debug("DELETE %s", url)
        resp = requests.delete(url, auth=self._auth, headers=self._headers)
        _log.debug("DELETE %s → %s", url, resp.status_code)
        _raise(resp)


# ------------------------------------------------------------------
# Atlassian Document Format helpers
# ------------------------------------------------------------------

def _text_to_adf(text: str) -> dict:
    """Convert plain text to a minimal ADF document (paragraphs on double newline)."""
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    return {
        "type": "doc",
        "version": 1,
        "content": [
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": para}],
            }
            for para in paragraphs
        ],
    }


def _adf_to_text(adf: dict) -> str:
    """Extract plain text from an ADF document (best-effort)."""
    parts: list[str] = []
    for block in adf.get("content", []):
        block_text = "".join(
            node.get("text", "") for node in block.get("content", [])
            if node.get("type") == "text"
        )
        if block_text:
            parts.append(block_text)
    return "\n\n".join(parts)


def _raise(resp: requests.Response) -> None:
    try:
        resp.raise_for_status()
    except requests.HTTPError as e:
        # Include the response body for better error messages
        try:
            detail = resp.json()
        except Exception:
            detail = resp.text
        raise requests.HTTPError(
            f"{e} — {detail}", response=resp
        ) from None
