"""Tests for cleanup_dead_window_binding — bot deletes the Telegram topic
when the bound tmux window disappears.
"""

from unittest.mock import AsyncMock, patch

import pytest
from telegram.error import BadRequest, TelegramError

import ccbot.handlers.status_polling as sp


class TestCleanupDeadWindow:
    @pytest.mark.asyncio
    async def test_unbinds_purges_and_deletes_topic(self):
        bot = AsyncMock()
        bot.delete_forum_topic = AsyncMock()

        with (
            patch.object(sp, "session_manager") as mock_sm,
            patch("ccbot.handlers.status_polling.clear_topic_state", new=AsyncMock()),
        ):
            mock_sm.resolve_chat_id.return_value = -1001
            mock_sm.remove_session_map_entry = AsyncMock()

            await sp.cleanup_dead_window_binding(bot, 1, 42, "@5")

            mock_sm.resolve_chat_id.assert_called_once_with(1, 42)
            mock_sm.unbind_thread.assert_called_once_with(1, 42)
            mock_sm.purge_window.assert_called_once_with("@5")
            mock_sm.remove_session_map_entry.assert_awaited_once_with("@5")
            bot.delete_forum_topic.assert_awaited_once_with(
                chat_id=-1001, message_thread_id=42
            )

    @pytest.mark.asyncio
    async def test_swallows_topic_gone_error(self):
        bot = AsyncMock()
        bot.delete_forum_topic = AsyncMock(
            side_effect=BadRequest("Message thread not found")
        )

        with (
            patch.object(sp, "session_manager") as mock_sm,
            patch("ccbot.handlers.status_polling.clear_topic_state", new=AsyncMock()),
        ):
            mock_sm.resolve_chat_id.return_value = -1001
            mock_sm.remove_session_map_entry = AsyncMock()

            # Must not raise — topic already gone is a no-op success.
            await sp.cleanup_dead_window_binding(bot, 1, 42, "@5")

            mock_sm.unbind_thread.assert_called_once_with(1, 42)

    @pytest.mark.asyncio
    async def test_logs_and_continues_on_other_telegram_error(self):
        bot = AsyncMock()
        bot.delete_forum_topic = AsyncMock(side_effect=TelegramError("flood"))

        with (
            patch.object(sp, "session_manager") as mock_sm,
            patch("ccbot.handlers.status_polling.clear_topic_state", new=AsyncMock()),
        ):
            mock_sm.resolve_chat_id.return_value = -1001
            mock_sm.remove_session_map_entry = AsyncMock()

            await sp.cleanup_dead_window_binding(bot, 1, 42, "@5")

            # State was still torn down even if topic deletion failed.
            mock_sm.unbind_thread.assert_called_once_with(1, 42)
            mock_sm.purge_window.assert_called_once_with("@5")
