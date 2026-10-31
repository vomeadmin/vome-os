"""
vomeos/integrations/buffer_social.py

The connector to Buffer, where Vome's social posts are scheduled.

The video studio writes a caption per platform for every delivered video. On
a person's "schedule it" in the thread, each platform's post is created here
for its slot in the posting calendar (`video_social.py`). Vome's social team
sees every post in Buffer's own calendar and can move or edit it there; this
connector never edits or deletes a post once created.

Sammy, 2026-10-30: Buffer over Zoho Social. Zoho's API is off until its
support enables it per portal, and its docs do not cover video. Buffer's is
self serve on every plan (free: three channels, 3,000 requests a month) and
takes a video by public link, which is how Studio publishes its renders.

THE API (developers.buffer.com, checked 2026-10-30)

  * One GraphQL endpoint, https://api.buffer.com, with
    `Authorization: Bearer <key>`. GraphQL errors arrive as HTTP 200 with an
    `errors` list, so `_graphql` turns them into a failed result.
  * `createPost(input: CreatePostInput!)` takes text, channelId,
    schedulingType (automatic), mode (customScheduled, with dueAt in UTC ISO
    8601), assets ([{video: {url, metadata}}]) and per-network metadata. It
    answers a PostActionSuccess or a MutationError.
  * Buffer fetches the video from its link when the post publishes, so the
    link has to stay up until then. R2 links do.
  * A key belongs to the organization owner who made it.

Environment:

    BUFFER_API_KEY           from publish.buffer.com/settings/api
    BUFFER_ORGANIZATION_ID   optional; the account's first organization
                             otherwise
    BUFFER_API_URL           default https://api.buffer.com
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from vomeos import config
from vomeos.integrations.base import Connector, IntegrationResult


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


_ORGANIZATIONS = "query { account { organizations { id name } } }"

_CHANNELS = """
query Channels($org: OrganizationId!) {
  channels(input: {organizationId: $org}) {
    id name displayName service isDisconnected
  }
}"""

_CREATE_POST = """
mutation CreatePost($input: CreatePostInput!) {
  createPost(input: $input) {
    __typename
    ... on PostActionSuccess { post { id dueAt } }
    ... on MutationError { message }
  }
}"""

_DESCRIBE = """
query Describe($name: String!) {
  __type(name: $name) {
    name kind
    enumValues { name }
    inputFields { name type { kind name ofType { kind name } } }
  }
}"""


@dataclass
class BufferSocial(Connector):
    """Create Vome's social posts in Buffer."""

    system: str = "buffer"
    base_url: str = field(
        default_factory=lambda: _env("BUFFER_API_URL",
                                     "https://api.buffer.com"))
    timeout: int = field(default=config.INTEGRATION_TIMEOUT_SECONDS)
    _org: str = field(default="", repr=False)

    def configured(self) -> bool:
        return bool(self.base_url) and bool(_env("BUFFER_API_KEY"))

    def ready_to_post(self) -> bool:
        """A key is all it needs: the organization and channels are found
        with it."""
        return self.configured()

    def headers(self) -> dict[str, str]:
        return {"Accept": "application/json",
                "Content-Type": "application/json",
                "Authorization": "Bearer %s" % _env("BUFFER_API_KEY")}

    def _graphql(self, operation: str, query: str,
                 variables: dict | None = None) -> IntegrationResult:
        """One GraphQL call. `data` is the response's `data` object."""
        res = self._call(operation, "POST", "/",
                         json_body={"query": query,
                                    "variables": variables or {}})
        if not res.ok:
            return res
        body = res.data if isinstance(res.data, dict) else {}
        if body.get("errors"):
            res.ok = False
            res.error = "; ".join(
                str(e.get("message", e)) if isinstance(e, dict) else str(e)
                for e in body["errors"])[:1000]
        res.data = body.get("data") or {}
        return res

    # -----------------------------------------------------------------
    # Operations. One method per thing the OS is allowed to ask for.
    # -----------------------------------------------------------------

    def organizations(self) -> IntegrationResult:
        """The organizations the key can see. Read only."""
        res = self._graphql("organizations", _ORGANIZATIONS)
        if res.ok:
            res.data = ((res.data.get("account") or {})
                        .get("organizations") or [])
        return res

    def organization_id(self) -> tuple[str, str]:
        """(id, "") for BUFFER_ORGANIZATION_ID or the first organization,
        else ("", why)."""
        if _env("BUFFER_ORGANIZATION_ID"):
            return _env("BUFFER_ORGANIZATION_ID"), ""
        if self._org:
            return self._org, ""
        res = self.organizations()
        if not res.ok:
            return "", res.error or "could not list Buffer organizations"
        if not res.data:
            return "", "the Buffer key sees no organization"
        self._org = str(res.data[0].get("id") or "")
        return self._org, ""

    def channels(self) -> IntegrationResult:
        """The organization's channels: id, name, service, isDisconnected.
        Read only."""
        org, why = self.organization_id()
        if not org:
            return IntegrationResult(self.system, "channels", False,
                                     error=why)
        res = self._graphql("channels", _CHANNELS, {"org": org})
        if res.ok:
            res.data = res.data.get("channels") or []
        return res

    def create_post(self, post: dict) -> IntegrationResult:
        """Create one post. Outward facing.

        Only called from a person's explicit "schedule it" in the video
        thread, or the check script on a person's say so. `data` is the
        `createPost` answer: {"__typename", "post": {"id", "dueAt"}} or
        {"__typename": "MutationError", "message"}.
        """
        res = self._graphql("create_post", _CREATE_POST, {"input": post})
        if res.ok:
            res.data = res.data.get("createPost") or {}
        return res

    def describe(self, type_name: str) -> IntegrationResult:
        """One schema type's enum values or input fields, for the check
        script. Read only."""
        res = self._graphql("describe", _DESCRIBE, {"name": type_name})
        if res.ok:
            res.data = res.data.get("__type") or {}
        return res
