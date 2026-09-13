"""
rainyai/proxy_manager.py
Centralized Discord route manager for RainyAI.

Starts in DIRECT mode. Switches to configured HTTP proxies when Discord
gateway/login connectivity fails. Returns to DIRECT when the direct route
is confirmed healthy again.

Singleton ``discord_proxy_manager`` is initialized by rainyai/core.py and
imported by events.py, guildsmanager.py, and webhook.py.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

_LOG = "[ProxyManager]"


def _safe_label(proxy_url: str) -> str:
    """Return 'host:port' only -- credentials are never logged."""
    try:
        p = urlparse(proxy_url)
        return f"{p.hostname}:{p.port}"
    except Exception:
        return "<proxy>"


@dataclass
class _ProxyHealth:
    consecutive_failures: int = 0
    last_failure: float = 0.0
    cooldown_until: float = 0.0

    def in_cooldown(self) -> bool:
        return time.monotonic() < self.cooldown_until

    def enter_cooldown(self, seconds: int) -> None:
        self.consecutive_failures += 1
        self.last_failure = time.monotonic()
        self.cooldown_until = time.monotonic() + seconds

    def reset(self) -> None:
        self.consecutive_failures = 0
        self.cooldown_until = 0.0


class DiscordProxyManager:
    """
    Single source of truth for the current Discord REST + Gateway route.

    _active_index == -1  ->  DIRECT (no proxy)
    _active_index >= 0   ->  index into self._proxies
    """

    def __init__(self, proxies: list, config: dict) -> None:
        self._proxies: list = [p for p in proxies if p]
        self._health: list = [_ProxyHealth() for _ in self._proxies]

        self._active_index: int = -1
        self._direct_failures: int = 0
        self._direct_recovery_successes: int = 0
        self._recovery_task = None
        self._bot = None  # Set via set_bot() after bot object is created.

        self._failure_threshold: int = int(config.get("failure_threshold", 3))
        self._proxy_cooldown: int = int(config.get("proxy_cooldown", 300))
        self._recovery_interval: int = int(config.get("recovery_interval", 600))
        self._recovery_threshold: int = int(config.get("recovery_threshold", 3))

        n = len(self._proxies)
        print(f"{_LOG} Starting in DIRECT mode. {n} proxy(ies) configured.")

    # ------------------------------------------------------------------ #
    # Public API                                                           #
    # ------------------------------------------------------------------ #

    def set_bot(self, bot) -> None:
        """Register the discord.py Bot instance after it is created."""
        self._bot = bot

    def get_active_proxy(self) -> Optional[str]:
        """Return None for direct, or the active proxy URL string."""
        if self._active_index < 0 or self._active_index >= len(self._proxies):
            return None
        return self._proxies[self._active_index]

    def get_requests_proxies(self) -> dict:
        """Return a dict usable as requests.Session.proxies."""
        proxy = self.get_active_proxy()
        if not proxy:
            return {}
        return {"http": proxy, "https": proxy}

    def is_gateway_connectivity_error(self, exc: BaseException) -> bool:
        """
        Return True only for genuine network-path failures seen during
        Discord Gateway login or connection.

        Returns False for normal Discord API errors, auth errors,
        per-route rate limits, invite failures, webhook failures,
        and any other application-level error.
        """
        import aiohttp
        from discord import HTTPException, LoginFailure

        # Auth errors are never a network/proxy problem.
        if isinstance(exc, LoginFailure):
            return False

        # Pure network / connection failures.
        if isinstance(exc, (aiohttp.ClientConnectorError,
                             aiohttp.ServerDisconnectedError)):
            return True

        if isinstance(exc, aiohttp.ClientError):
            msg = str(exc).lower()
            return any(k in msg for k in (
                "cannot connect", "connection refused",
                "timed out", "network unreachable",
            ))

        if isinstance(exc, asyncio.TimeoutError):
            return True

        if isinstance(exc, OSError):
            # ECONNREFUSED=111/10061, ENETUNREACH=101/10051,
            # ECONNRESET=104/10054, ETIMEDOUT=110/10060
            return exc.errno in (54, 104, 110, 111, 113, 10051, 10054, 10060, 10061)

        if isinstance(exc, HTTPException):
            # Cloudflare IP ban -- genuine connectivity failure.
            text = str(exc).lower()
            if "1015" in text or "cloudflare" in text:
                return True
            # All other HTTP errors (400, 401, 403, 404, normal 429) are API errors.
            return False

        # String-match covering existing RainyAI error descriptions.
        text = str(exc)
        if (
            "Cannot connect to host discord.com" in text
            or "Cannot connect to host gateway.discord.gg" in text
            or "ClientConnectorError" in type(exc).__name__
        ):
            return True

        return False

    def record_gateway_failure(self, exc: BaseException) -> bool:
        """
        Record a Discord gateway/login connectivity failure.
        Returns True if a proxy was activated (caller may skip long waits).
        Only call this for errors that passed is_gateway_connectivity_error().
        """
        if self._active_index == -1:
            self._direct_failures += 1
            print(
                f"{_LOG} Direct Gateway failure "
                f"{self._direct_failures}/{self._failure_threshold}."
            )
            if self._direct_failures >= self._failure_threshold:
                return self._activate_next_proxy()
            return False
        else:
            idx = self._active_index
            label = _safe_label(self._proxies[idx])
            self._health[idx].enter_cooldown(self._proxy_cooldown)
            print(
                f"{_LOG} PROXY_{idx + 1} ({label}) failed. "
                f"Cooldown {self._proxy_cooldown}s."
            )
            return self._activate_next_proxy()

    def record_gateway_success(self) -> None:
        """
        Record a successful Discord Gateway connection (call from on_ready).
        Resets the failure counter and starts the direct-recovery loop if on
        a proxy.
        """
        if self._active_index == -1:
            if self._direct_failures > 0:
                print(f"{_LOG} Direct Gateway healthy. Counter reset.")
            self._direct_failures = 0
        else:
            idx = self._active_index
            self._health[idx].reset()
            label = _safe_label(self._proxies[idx])
            print(f"{_LOG} PROXY_{idx + 1} ({label}) healthy. Starting recovery loop.")
            self._ensure_recovery_loop()

    # ------------------------------------------------------------------ #
    # Internal switching                                                   #
    # ------------------------------------------------------------------ #

    def _activate_next_proxy(self) -> bool:
        """
        Find the next available proxy (not in cooldown) and activate it.
        Searches starting from the proxy after the currently active one.
        Returns True if a proxy was found and activated.
        """
        n = len(self._proxies)
        if n == 0:
            print(f"{_LOG} No proxies configured. Cannot failover.")
            return False

        start = self._active_index + 1
        for offset in range(n):
            idx = (start + offset) % n
            if not self._health[idx].in_cooldown():
                self._do_switch(idx)
                return True

        print(f"{_LOG} All proxies are in cooldown. Will retry later.")
        return False

    def _do_switch(self, new_index: int) -> None:
        """Perform the actual route switch to proxy at new_index."""
        old = "DIRECT" if self._active_index < 0 else f"PROXY_{self._active_index + 1}"
        new = f"PROXY_{new_index + 1}"
        label = _safe_label(self._proxies[new_index])

        self._active_index = new_index
        self._direct_failures = 0
        self._direct_recovery_successes = 0

        # Update discord.py HTTP client so all subsequent requests use this proxy.
        if self._bot is not None:
            self._bot.http.proxy = self._proxies[new_index]

        print(f"{_LOG} Switching {old} -> {new} ({label}).")

        # Close WS so discord.py reconnects using the updated proxy.
        # Code 1000 is the "normal close" code.
        # Verified against discord.py 2.6.3 client.py:769-778:
        #   ConnectionClosed(code=1000) does NOT call close() or raise --
        #   it falls through to the backoff+retry path in connect()'s loop,
        #   where the new ws_connect() call reads bot.http.proxy.
        self._schedule_ws_reconnect()
        self._ensure_recovery_loop()

    def switch_to_direct(self) -> None:
        """Switch back to DIRECT mode."""
        old = f"PROXY_{self._active_index + 1}" if self._active_index >= 0 else "DIRECT"
        self._active_index = -1
        self._direct_failures = 0
        self._direct_recovery_successes = 0

        if self._bot is not None:
            self._bot.http.proxy = None

        print(f"{_LOG} Direct connectivity recovered. Switching {old} -> DIRECT.")
        self._schedule_ws_reconnect()

    def _schedule_ws_reconnect(self) -> None:
        """Schedule WS close as a fire-and-forget asyncio task."""
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(self._close_ws_for_reconnect())
        except RuntimeError:
            pass  # No running loop -- bot will use new proxy on next start().

    async def _close_ws_for_reconnect(self) -> None:
        """
        Close the Gateway WebSocket with code 1000 (normal close).

        discord.py 2.6.3 connect() (client.py:769-778) treats code 1000 as
        reconnectable: it does NOT call self.close() or raise. Instead it
        falls through to: retry = backoff.delay(); await asyncio.sleep(retry)
        then loops and calls DiscordWebSocket.from_client() again, which calls
        http.ws_connect() with the already-updated bot.http.proxy.
        """
        try:
            if self._bot and self._bot.ws and self._bot.ws.open:
                await self._bot.ws.close(code=1000)
        except Exception:
            pass  # WS may already be closed or bot not yet connected.

    # ------------------------------------------------------------------ #
    # Direct recovery loop                                                 #
    # ------------------------------------------------------------------ #

    def _ensure_recovery_loop(self) -> None:
        """Start direct recovery task if not already running."""
        if self._recovery_task is None or self._recovery_task.done():
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    self._recovery_task = loop.create_task(
                        self._direct_recovery_loop()
                    )
            except RuntimeError:
                pass

    async def _direct_recovery_loop(self) -> None:
        """
        Periodically test direct connectivity while on a proxy.
        Switches back to DIRECT only after recovery_threshold consecutive
        successful checks. Conservative interval (default 600 s / 10 min)
        to avoid spamming Discord.
        """
        while self._active_index >= 0:
            await asyncio.sleep(self._recovery_interval)

            if self._active_index < 0:
                break  # Already switched back.

            print(f"{_LOG} Testing direct Discord connectivity...")
            ok = await self._test_direct_connectivity()

            if ok:
                self._direct_recovery_successes += 1
                n = self._direct_recovery_successes
                total = self._recovery_threshold
                print(f"{_LOG} Direct recovery check succeeded ({n}/{total}).")
                if self._direct_recovery_successes >= self._recovery_threshold:
                    self.switch_to_direct()
                    break
            else:
                if self._direct_recovery_successes > 0:
                    print(f"{_LOG} Direct recovery check failed. Resetting counter.")
                self._direct_recovery_successes = 0

    async def _test_direct_connectivity(self) -> bool:
        """
        Lightweight GET /gateway without proxy and without a bot token.
        proxy=None is explicit -- bypasses any environment-level proxy settings.
        Strict 10 s timeout.
        """
        import aiohttp
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    "https://discord.com/api/v10/gateway",
                    proxy=None,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    return resp.status == 200
        except Exception:
            return False


# Module-level singleton -- initialized by rainyai/core.py.
discord_proxy_manager: Optional[DiscordProxyManager] = None
