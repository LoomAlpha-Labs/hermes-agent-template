"""Behavior regressions for Jesse's in-place Telegram recovery."""
import asyncio
from unittest.mock import AsyncMock, MagicMock
import httpx
import pytest
from gateway.config import PlatformConfig
from plugins.platforms.telegram.adapter import TelegramAdapter
from plugins.platforms.telegram import telegram_network as net

@pytest.mark.asyncio
async def test_reply_waits_for_polling_proof_and_is_sent_once():
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))
    adapter._running = True
    adapter._rich_send_disabled = True
    adapter.send_typing = AsyncMock()
    adapter._bot = MagicMock()
    adapter._bot.send_message = AsyncMock(return_value=MagicMock(message_id=42))
    adapter.gateway_runner = None
    generation, progress = adapter._begin_polling_generation()
    pending = asyncio.create_task(adapter.send("123", "reply"))
    await asyncio.sleep(0)
    adapter._bot.send_message.assert_not_awaited()
    assert not pending.done()
    adapter._record_polling_progress(generation)
    result = await asyncio.wait_for(pending, 2)
    assert result.success and result.message_id == "42"
    adapter._bot.send_message.assert_awaited_once()

@pytest.mark.asyncio
async def test_in_place_recovery_replays_ledger_once_for_owner():
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))
    adapter._owner_profile = "owner"
    adapter.gateway_runner = MagicMock()
    replay = adapter.gateway_runner._redeliver_failed_obligations_for_platform = AsyncMock(return_value=0)
    generation, _ = adapter._begin_polling_generation()
    assert not adapter._record_polling_progress(generation - 1)
    assert adapter._record_polling_progress(generation)
    assert adapter._record_polling_progress(generation)
    await asyncio.gather(*adapter._background_tasks)
    replay.assert_awaited_once_with(adapter.platform, profile="owner")

@pytest.mark.asyncio
async def test_pool_exhaustion_uses_another_path_without_replaying_sent_request(monkeypatch):
    calls = []
    closed = []
    class Transport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            calls.append(request.url.host)
            if request.url.host == "149.154.166.110":
                raise httpx.PoolTimeout("not sent")
            return httpx.Response(200, text="ok")
        async def aclose(self):
            closed.append(self)
    monkeypatch.setattr(net.httpx, "AsyncHTTPTransport", lambda **kw: Transport())
    transport = net.TelegramFallbackTransport(["149.154.166.110", "149.154.167.220"])
    response = await transport.handle_async_request(httpx.Request("POST", "https://api.telegram.org/botTEST/getMe"))
    assert response.status_code == 200
    assert calls == ["149.154.166.110", "149.154.167.220"]
    assert closed == []  # Do not abort other sends occupying the busy pool.
    assert not net._is_retryable_connect_error(httpx.ReadTimeout("ambiguous send"))
    await transport.aclose()
