"""
OpenClaw Webhook — the bridge between Pulse and the agent.

Supports two session modes:
- "main"/"persistent": Reuses the configured OpenClaw session
- "isolated": Spawns a separate hook session that doesn't compete with
  the main conversation. Results can be announced back to the channel.

The isolated approach lets Pulse-triggered work happen in the background
while the main session stays clean for human conversation.
"""

import asyncio
import json
import logging
import re
from typing import Optional
from urllib.parse import urlparse
from uuid import uuid4

import aiohttp

from pulse.src.core.config import PulseConfig

logger = logging.getLogger("pulse.webhook")
HOOK_REQUEST_TIMEOUT_SECONDS = 20
HOOK_MAX_ATTEMPTS = 2


def _ssl_for_url(url: str):
    """Accept the Gateway's self-signed TLS certificate on loopback only."""
    parsed = urlparse(url)
    if parsed.scheme == "https" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}:
        return False
    return None


class OpenClawWebhook:
    """Triggers OpenClaw agent turns via webhook."""

    def __init__(self, config: PulseConfig):
        self.url = config.openclaw.webhook_url
        self.token = config.openclaw.webhook_token
        self.session_mode = config.openclaw.session_mode
        self.deliver = config.openclaw.deliver
        self.isolated_model = config.openclaw.isolated_model
        self.session_key = config.openclaw.session_key
        self.result_callback_url = f"http://127.0.0.1:{config.daemon.health_port}/openclaw/turn-result"
        self._session: Optional[aiohttp.ClientSession] = None

        # DEBUG: логируем, что мы вообще получили из конфига
        logger.info(
            "[PULSE→HOOK INIT] session_mode=%s session_key=%s",
            self.session_mode,
            self.session_key,
        )

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def trigger(self, message: str, name: str = "Pulse") -> bool | None:
        """
        Trigger an agent turn via OpenClaw webhook.

        In isolated mode, the hook session runs separately from the main
        conversation. The agent gets full tool access and can announce
        results back to the channel when done.

        Args:
            message: The prompt/context for the agent turn
            name: Human-readable name for the hook

        Returns:
            True for accepted admission, False for proven non-admission, and
            None when delivery remains ambiguous after one same-key replay.
        """
        session = await self._get_session()

        headers = {
            "Content-Type": "application/json",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request_id = f"pulse-{uuid4().hex}"
        headers["Idempotency-Key"] = request_id

        payload = self._build_payload(message=message, name=name)

        # DEBUG: логируем точный payload до отправки (включая sessionKey/channel/to)
        try:
            safe_headers = {k: ("***" if k.lower() == "authorization" else v) for k, v in headers.items()}
            logger.info("[PULSE→HOOK] url=%s headers=%s payload_json=%s", self.url, safe_headers, json.dumps(payload, ensure_ascii=False))
        except Exception as log_err:
            logger.warning(f"Failed to log webhook payload: {log_err}")

        for attempt in range(1, HOOK_MAX_ATTEMPTS + 1):
            try:
                async with session.post(
                    self.url,
                    json=payload,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=HOOK_REQUEST_TIMEOUT_SECONDS),
                    ssl=_ssl_for_url(self.url),
                ) as resp:
                    if resp.status in (200, 202):
                        try:
                            body = await resp.json()
                            run_id = body.get("runId")
                        except Exception as exc:
                            logger.warning(
                                "Webhook acceptance body unreadable "
                                "(attempt=%s/%s, request_id=%s): %s",
                                attempt,
                                HOOK_MAX_ATTEMPTS,
                                request_id,
                                exc,
                            )
                            if attempt < HOOK_MAX_ATTEMPTS:
                                continue
                            return None

                        if not isinstance(run_id, str) or not run_id.strip():
                            logger.warning(
                                "Webhook acceptance missing runId "
                                "(attempt=%s/%s, request_id=%s)",
                                attempt,
                                HOOK_MAX_ATTEMPTS,
                                request_id,
                            )
                            if attempt < HOOK_MAX_ATTEMPTS:
                                continue
                            return None

                        mode_str = (
                            "isolated"
                            if self.session_mode == "isolated"
                            else "persistent"
                        )
                        logger.info(
                            "Webhook accepted — mode=%s, runId=%s, request_id=%s, "
                            "attempt=%s",
                            mode_str,
                            run_id,
                            request_id,
                            attempt,
                        )
                        return True

                    body = await resp.text()
                    logger.warning(
                        "Webhook explicitly rejected request_id=%s with %s: %s",
                        request_id,
                        resp.status,
                        body[:200],
                    )
                    return False

            except (aiohttp.ClientConnectorError, aiohttp.InvalidURL) as exc:
                logger.error(
                    "Webhook not sent (request_id=%s): %s", request_id, exc
                )
                return False
            except (asyncio.TimeoutError, aiohttp.ClientConnectionError) as exc:
                logger.warning(
                    "Webhook outcome ambiguous (attempt=%s/%s, request_id=%s): %s",
                    attempt,
                    HOOK_MAX_ATTEMPTS,
                    request_id,
                    exc,
                )
                if attempt < HOOK_MAX_ATTEMPTS:
                    continue
                return None
            except aiohttp.ClientError as exc:
                logger.error(
                    "Webhook client outcome ambiguous (request_id=%s): %s",
                    request_id,
                    exc,
                )
                return None
            except Exception as exc:
                logger.error(
                    "Webhook unexpected outcome ambiguous (request_id=%s): %s",
                    request_id,
                    exc,
                )
                return None

        return None

    def _build_payload(self, message: str, name: str) -> dict:
        """Translate Pulse session semantics to the current OpenClaw hook API."""
        payload = {
            "message": message,
            "name": name,
            "wakeMode": "now",
            "deliver": self.deliver,
        }

        result_callback_kind = self._result_callback_kind(message)
        if result_callback_kind:
            payload["resultCallback"] = {
                "url": self.result_callback_url,
                "kind": result_callback_kind,
            }
            if self.token:
                payload["resultCallback"]["token"] = self.token

        # "main" is Pulse's legacy name for OpenClaw's persistent hook session.
        if self.session_mode in {"main", "persistent"}:
            payload["sessionMode"] = "persistent"
            payload["sessionKey"] = self.session_key
            payload["channel"] = "telegram"
            payload["to"] = "312058326"
        else:
            payload["sessionMode"] = "isolated"
            if self.isolated_model:
                payload["model"] = self.isolated_model
        return payload

    def _result_callback_kind(self, message: str) -> str | None:
        if "EMOTIONAL LANDSCAPE" in message and "- Mode: write_diary_note" in message:
            return "pulse.emotions.write_diary_note"
        conversation = re.search(
            r"(?m)^PULSE_CONVERSATION_CALLBACK_KIND=(pulse\.conversation\.growth:[A-Za-z0-9._-]{1,128})$",
            message,
        )
        if conversation:
            return conversation.group(1)
        return None

    async def wake(self, text: str) -> bool:
        """
        Send a wake event (lighter than full agent turn).
        Uses /hooks/wake instead of /hooks/agent.
        """
        session = await self._get_session()
        parsed = urlparse(self.url)
        wake_url = f"{parsed.scheme}://{parsed.netloc}/hooks/wake"

        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        payload = {"text": text, "mode": "now"}

        try:
            async with session.post(
                wake_url,
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10),
                ssl=_ssl_for_url(wake_url),
            ) as resp:
                return resp.status == 200
        except Exception as e:
            logger.error(f"Wake error: {e}")
            return False

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
