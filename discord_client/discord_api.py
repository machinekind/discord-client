"""REST client for the Discord bot API: channels, threads, messages, search, posting."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from datetime import datetime

import httpx

API_BASE_URL = "https://discord.com/api/v10"
USER_AGENT = "DiscordBot (https://github.com/machinekind/discord-client, 0.1.0)"

SEARCH_NOT_INDEXED_CODE = 110000
SEARCH_MAX_LIMIT = 25  # Discord accepts 1 to 25 results per guild search

_PAGE_LIMIT = 100


class DiscordAPIError(RuntimeError):
    """A non-2xx Discord response not otherwise handled.

    Carries the method, URL, status, and response body: the API's own error
    body is far more useful than a bare status code.
    """

    def __init__(self, resp: httpx.Response) -> None:
        super().__init__(
            f"{resp.request.method} {resp.request.url} -> {resp.status_code}\n{resp.text}"
        )
        self.response = resp
        self.status_code = resp.status_code


class SearchNotIndexed(RuntimeError):
    """Discord code 110000: the guild hasn't finished indexing for search yet.

    Discord sends this code with 202 Accepted, not an error status, while it
    builds the index, so a caller that only checks the status would read the
    empty body as "no results".
    """

    def __init__(self, guild_id: str, message: str) -> None:
        super().__init__(f"guild {guild_id} is not indexed for search yet: {message}")
        self.guild_id = guild_id


def _safe_json(resp: httpx.Response) -> dict:
    try:
        body = resp.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _retry_after_seconds(resp: httpx.Response) -> float:
    body = _safe_json(resp)
    if "retry_after" in body:
        return float(body["retry_after"])
    header = resp.headers.get("Retry-After")
    return float(header) if header is not None else 1.0


class DiscordClient:
    """Thin wrapper over httpx.Client for the Discord bot REST API.

    Handles auth, the shared rate-limit rules, and loud failures. Nothing in
    here hardcodes a per-route limit; every route is throttled the same way
    from the response headers Discord sends back.
    """

    # The channel types this client handles (Discord's full enum is larger).
    TEXT = 0
    ANNOUNCEMENT = 5
    ANNOUNCEMENT_THREAD = 10
    PUBLIC_THREAD = 11
    PRIVATE_THREAD = 12
    FORUM = 15

    def __init__(
        self,
        token: str,
        *,
        sleep: Callable[[float], None] = time.sleep,
        timeout: float = 30.0,
    ) -> None:
        self._sleep = sleep
        self._client = httpx.Client(
            base_url=API_BASE_URL,
            headers={"Authorization": f"Bot {token}", "User-Agent": USER_AGENT},
            timeout=timeout,
        )

    def __enter__(self) -> DiscordClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    # -- transport ---------------------------------------------------------

    def _send(self, method: str, url: str, **kwargs: object) -> httpx.Response:
        """One HTTP call, with the single 429-retry and the rate-limit sleep.

        Returns the response even if it's an error; callers decide how to
        turn it into an exception (most go through `_request`, `search`
        intercepts code 110000 first).
        """
        resp = self._client.request(method, url, **kwargs)
        if resp.status_code == 429:
            self._sleep(_retry_after_seconds(resp))
            resp = self._client.request(method, url, **kwargs)
        if resp.headers.get("X-RateLimit-Remaining") == "0":
            reset_after = resp.headers.get("X-RateLimit-Reset-After")
            if reset_after is not None:
                self._sleep(float(reset_after))
        return resp

    def _request(self, method: str, url: str, **kwargs: object) -> httpx.Response:
        resp = self._send(method, url, **kwargs)
        if resp.is_error:
            raise DiscordAPIError(resp)
        return resp

    # -- identity -----------------------------------------------------------

    def me(self) -> dict:
        """The bot's own user object (GET /users/@me): id, username."""
        return self._request("GET", "/users/@me").json()

    def my_guilds(self) -> list[dict]:
        """The guilds the bot is a member of (GET /users/@me/guilds), first page only.

        One page holds 200 guilds. The only caller wants to know whether there
        is exactly one, so a bot in more than a page's worth needs no paging.
        """
        return self._request("GET", "/users/@me/guilds").json()

    # -- channels & threads --------------------------------------------------

    def guild_channels(self, guild_id: str) -> list[dict]:
        """All channels in a guild (GET /guilds/{id}/channels)."""
        return self._request("GET", f"/guilds/{guild_id}/channels").json()

    def channel(self, channel_id: str) -> dict:
        """One channel or thread object (GET /channels/{id}): id, type, name, parent_id.

        Type distinguishes a thread (10/11/12) from a normal text/forum channel,
        which callers need because a thread cannot contain a sub-thread.
        """
        return self._request("GET", f"/channels/{channel_id}").json()

    def active_threads(self, guild_id: str) -> list[dict]:
        """Active threads in a guild (GET /guilds/{id}/threads/active).

        Returns the "threads" array from Discord's response, dropping the
        accompanying thread-member list nothing here needs.
        """
        return self._request("GET", f"/guilds/{guild_id}/threads/active").json()["threads"]

    def archived_public_threads(
        self, channel_id: str, since: datetime | None = None
    ) -> list[dict]:
        """Archived public threads of one channel or forum, newest archive first.

        GET /channels/{id}/threads/archived/public. Thread messages never
        appear in the parent channel's own /messages, and a forum channel's
        posts exist only as threads, so this and `active_threads` are the
        only way to discover forum discussions.

        Discord pages this list by archive time, newest first. While a page
        says `has_more`, the next one is requested with `before` set to the
        archive timestamp of the last (oldest) thread on the page.

        `since`, an aware datetime, stops the paging early: once the oldest
        thread on a page was archived before `since`, no further page is
        read. A thread archived before `since` cannot hold a message posted
        at or after it, because posting in an archived thread unarchives it
        and a later archive would move its timestamp forward. The threads on
        the pages that were read are all returned, so the result can include
        a few archived before `since`. With `since=None` every page is read.
        """
        url = f"/channels/{channel_id}/threads/archived/public"
        params: dict[str, str | int] = {"limit": _PAGE_LIMIT}
        threads: list[dict] = []
        while True:
            body = self._request("GET", url, params=params).json()
            page = body["threads"]
            threads.extend(page)
            if not page or not body.get("has_more"):
                return threads
            oldest = page[-1]["thread_metadata"]["archive_timestamp"]
            if since is not None and datetime.fromisoformat(oldest) < since:
                return threads
            params = {"limit": _PAGE_LIMIT, "before": oldest}

    # -- messages -------------------------------------------------------------

    def messages(
        self,
        channel_id: str,
        after: str | None = None,
        before: str | None = None,
    ) -> Iterator[dict]:
        """Yield every message with snowflake in [after, before), oldest first.

        Discord's GET .../messages always returns one page newest-first, but
        paginating with `after` walks forward chronologically: each page
        holds the oldest still-unread slice, capped at `limit`, not the
        newest slice. To yield the whole range exactly once in a single
        well-defined order, each page is reversed before being yielded (so
        the generator's overall output is chronological, ascending by
        snowflake), and the next `after` cursor is the newest id in the page
        just read. `before` is enforced client-side rather than sent as a
        request parameter, since Discord treats `before`/`after` as mutually
        exclusive on one call.
        """
        cursor = after if after is not None else "0"
        before_int = int(before) if before is not None else None
        while True:
            page = self._request(
                "GET",
                f"/channels/{channel_id}/messages",
                params={"limit": _PAGE_LIMIT, "after": cursor},
            ).json()
            if not page:
                return
            page.reverse()
            for msg in page:
                if before_int is not None and int(msg["id"]) >= before_int:
                    return
                yield msg
            cursor = page[-1]["id"]
            if len(page) < _PAGE_LIMIT:
                return

    # -- search -----------------------------------------------------------

    def search(self, guild_id: str, query: str, limit: int = SEARCH_MAX_LIMIT) -> dict:
        """GET /guilds/{id}/messages/search. Discord accepts a `limit` of 1 to 25.

        Raises SearchNotIndexed whenever the body carries code 110000,
        whatever the status. Discord documents that an unindexed guild
        answers 202 Accepted with that code (plus `documents_indexed` and
        `retry_after`), so checking only error statuses would turn "not
        indexed yet" into an empty result. Any other error status raises
        DiscordAPIError.
        """
        resp = self._send(
            "GET",
            f"/guilds/{guild_id}/messages/search",
            params={"content": query, "limit": limit},
        )
        body = _safe_json(resp)
        if body.get("code") == SEARCH_NOT_INDEXED_CODE:
            raise SearchNotIndexed(guild_id, body.get("message", "not indexed"))
        if resp.is_error:
            raise DiscordAPIError(resp)
        return resp.json()

    # -- writes -------------------------------------------------------------
    #
    # Two helpers post, for library callers that want them. The CLI and the MCP
    # server never call either.

    def post_message(
        self,
        channel_id: str,
        content: str | None = None,
        embeds: list[dict] | None = None,
    ) -> dict:
        """POST /channels/{id}/messages. Always sends allowed_mentions: {"parse": []}."""
        body: dict = {"allowed_mentions": {"parse": []}}
        if content is not None:
            body["content"] = content
        if embeds is not None:
            body["embeds"] = embeds
        return self._request("POST", f"/channels/{channel_id}/messages", json=body).json()

    def start_thread(self, channel_id: str, message_id: str, name: str) -> dict:
        """POST /channels/{channel_id}/messages/{message_id}/threads.

        Carries no message content, but still sends allowed_mentions, so
        every write helper sends it and none can ping anyone by accident,
        including where it makes no difference today.
        """
        body = {"name": name, "allowed_mentions": {"parse": []}}
        return self._request(
            "POST",
            f"/channels/{channel_id}/messages/{message_id}/threads",
            json=body,
        ).json()
