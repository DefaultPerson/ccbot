"""Tests for auto_create_topics_for_orphans — scan tmux for unbound windows
and auto-create Telegram forum topics.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram.error import BadRequest

import ccbot.handlers.status_polling as sp


@pytest.fixture(autouse=True)
def _reset_module_state():
    sp._creating_topics.clear()
    sp._create_disabled_until = 0.0
    yield
    sp._creating_topics.clear()
    sp._create_disabled_until = 0.0


def _make_window(wid: str, name: str = "foo", cwd: str = "/home/u/foo") -> MagicMock:
    w = MagicMock()
    w.window_id = wid
    w.window_name = name
    w.cwd = cwd
    w.pane_current_command = "claude"
    return w


def _topic(thread_id: int = 99, name: str = "foo") -> MagicMock:
    t = MagicMock()
    t.message_thread_id = thread_id
    t.name = name
    return t


class TestAutoCreateTopics:
    @pytest.mark.asyncio
    async def test_orphan_creates_topic_and_binds(self):
        bot = AsyncMock()
        bot.create_forum_topic = AsyncMock(return_value=_topic(99, "foo"))
        bot.send_message = AsyncMock()

        with (
            patch.object(sp, "tmux_manager") as mock_tmux,
            patch.object(sp, "session_manager") as mock_sm,
            patch.object(sp, "config") as mock_config,
        ):
            mock_config.auto_create_topics = True
            mock_config.allowed_users = {1}
            mock_tmux.list_windows = AsyncMock(
                return_value=[_make_window("@7", "foo", "/home/u/foo")]
            )
            mock_tmux.rename_window = AsyncMock()
            mock_tmux.set_window_option = AsyncMock()
            mock_sm.iter_thread_bindings.return_value = iter([])
            mock_sm.is_unbind_blacklisted.return_value = False
            mock_sm.get_forum_chat_id.return_value = -1001

            await sp.auto_create_topics_for_orphans(bot)

            bot.create_forum_topic.assert_awaited_once_with(chat_id=-1001, name="foo")
            mock_sm.bind_thread.assert_called_once_with(1, 99, "@7", window_name="foo")
            mock_sm.set_group_chat_id.assert_called_once_with(1, 99, -1001)
            mock_tmux.set_window_option.assert_awaited_once_with(
                "@7", "allow-rename", "off"
            )

    @pytest.mark.asyncio
    async def test_bound_window_skipped(self):
        bot = AsyncMock()
        bot.create_forum_topic = AsyncMock()

        with (
            patch.object(sp, "tmux_manager") as mock_tmux,
            patch.object(sp, "session_manager") as mock_sm,
            patch.object(sp, "config") as mock_config,
        ):
            mock_config.auto_create_topics = True
            mock_config.allowed_users = {1}
            mock_tmux.list_windows = AsyncMock(
                return_value=[_make_window("@7", "foo", "/home/u/foo")]
            )
            mock_sm.iter_thread_bindings.return_value = iter([(1, 42, "@7")])
            mock_sm.is_unbind_blacklisted.return_value = False
            mock_sm.get_forum_chat_id.return_value = -1001

            await sp.auto_create_topics_for_orphans(bot)

            bot.create_forum_topic.assert_not_called()
            mock_sm.bind_thread.assert_not_called()

    @pytest.mark.asyncio
    async def test_blacklisted_window_skipped(self):
        bot = AsyncMock()
        bot.create_forum_topic = AsyncMock()

        with (
            patch.object(sp, "tmux_manager") as mock_tmux,
            patch.object(sp, "session_manager") as mock_sm,
            patch.object(sp, "config") as mock_config,
        ):
            mock_config.auto_create_topics = True
            mock_config.allowed_users = {1}
            mock_tmux.list_windows = AsyncMock(
                return_value=[_make_window("@7", "foo", "/home/u/foo")]
            )
            mock_sm.iter_thread_bindings.return_value = iter([])
            mock_sm.is_unbind_blacklisted.side_effect = lambda wid: wid == "@7"
            mock_sm.get_forum_chat_id.return_value = -1001

            await sp.auto_create_topics_for_orphans(bot)

            bot.create_forum_topic.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_forum_chat_id_skips_all(self):
        bot = AsyncMock()
        bot.create_forum_topic = AsyncMock()

        with (
            patch.object(sp, "tmux_manager") as mock_tmux,
            patch.object(sp, "session_manager") as mock_sm,
            patch.object(sp, "config") as mock_config,
        ):
            mock_config.auto_create_topics = True
            mock_config.allowed_users = {1, 2}
            mock_config.forum_chat_id = None
            mock_tmux.list_windows = AsyncMock(
                return_value=[_make_window("@7", "foo", "/home/u/foo")]
            )
            mock_sm.iter_thread_bindings.return_value = iter([])
            mock_sm.is_unbind_blacklisted.return_value = False
            mock_sm.get_forum_chat_id.return_value = None

            await sp.auto_create_topics_for_orphans(bot)

            bot.create_forum_topic.assert_not_called()

    @pytest.mark.asyncio
    async def test_forum_chat_id_env_overrides_when_user_map_empty(self):
        bot = AsyncMock()
        bot.create_forum_topic = AsyncMock(return_value=_topic(99, "foo"))
        bot.send_message = AsyncMock()

        with (
            patch.object(sp, "tmux_manager") as mock_tmux,
            patch.object(sp, "session_manager") as mock_sm,
            patch.object(sp, "config") as mock_config,
        ):
            mock_config.auto_create_topics = True
            mock_config.allowed_users = {1}
            mock_config.forum_chat_id = -1009999
            mock_tmux.list_windows = AsyncMock(
                return_value=[_make_window("@7", "foo", "/home/u/foo")]
            )
            mock_tmux.rename_window = AsyncMock()
            mock_tmux.set_window_option = AsyncMock()
            mock_sm.iter_thread_bindings.return_value = iter([])
            mock_sm.is_unbind_blacklisted.return_value = False
            mock_sm.get_forum_chat_id.return_value = None

            await sp.auto_create_topics_for_orphans(bot)

            bot.create_forum_topic.assert_awaited_once_with(
                chat_id=-1009999, name="foo"
            )
            mock_sm.bind_thread.assert_called_once_with(1, 99, "@7", window_name="foo")

    @pytest.mark.asyncio
    async def test_auto_create_disabled_returns_early(self):
        bot = AsyncMock()

        with (
            patch.object(sp, "tmux_manager") as mock_tmux,
            patch.object(sp, "config") as mock_config,
        ):
            mock_config.auto_create_topics = False
            mock_tmux.list_windows = AsyncMock()

            await sp.auto_create_topics_for_orphans(bot)

            mock_tmux.list_windows.assert_not_called()

    @pytest.mark.asyncio
    async def test_window_name_fallback_to_cwd(self):
        bot = AsyncMock()
        bot.create_forum_topic = AsyncMock(return_value=_topic(99, "myrepo"))
        bot.send_message = AsyncMock()

        with (
            patch.object(sp, "tmux_manager") as mock_tmux,
            patch.object(sp, "session_manager") as mock_sm,
            patch.object(sp, "config") as mock_config,
        ):
            mock_config.auto_create_topics = True
            mock_config.allowed_users = {1}
            mock_tmux.list_windows = AsyncMock(
                return_value=[_make_window("@7", "bash", "/home/u/myrepo")]
            )
            mock_tmux.rename_window = AsyncMock()
            mock_tmux.set_window_option = AsyncMock()
            mock_sm.iter_thread_bindings.return_value = iter([])
            mock_sm.is_unbind_blacklisted.return_value = False
            mock_sm.get_forum_chat_id.return_value = -1001

            await sp.auto_create_topics_for_orphans(bot)

            bot.create_forum_topic.assert_awaited_once_with(
                chat_id=-1001, name="myrepo"
            )
            mock_tmux.rename_window.assert_awaited_once_with("@7", "myrepo")

    @pytest.mark.asyncio
    async def test_window_name_final_fallback_to_wid(self):
        bot = AsyncMock()
        bot.create_forum_topic = AsyncMock(return_value=_topic(99, "tmux-@7"))
        bot.send_message = AsyncMock()

        with (
            patch.object(sp, "tmux_manager") as mock_tmux,
            patch.object(sp, "session_manager") as mock_sm,
            patch.object(sp, "config") as mock_config,
        ):
            mock_config.auto_create_topics = True
            mock_config.allowed_users = {1}
            mock_tmux.list_windows = AsyncMock(
                return_value=[_make_window("@7", "bash", "")]
            )
            mock_tmux.rename_window = AsyncMock()
            mock_tmux.set_window_option = AsyncMock()
            mock_sm.iter_thread_bindings.return_value = iter([])
            mock_sm.is_unbind_blacklisted.return_value = False
            mock_sm.get_forum_chat_id.return_value = -1001

            await sp.auto_create_topics_for_orphans(bot)

            bot.create_forum_topic.assert_awaited_once_with(
                chat_id=-1001, name="tmux-@7"
            )

    @pytest.mark.asyncio
    async def test_permission_error_throttles_for_an_hour(self):
        bot = AsyncMock()
        bot.create_forum_topic = AsyncMock(
            side_effect=BadRequest("not enough rights to manage topics")
        )

        with (
            patch.object(sp, "tmux_manager") as mock_tmux,
            patch.object(sp, "session_manager") as mock_sm,
            patch.object(sp, "config") as mock_config,
        ):
            mock_config.auto_create_topics = True
            mock_config.allowed_users = {1}
            mock_tmux.list_windows = AsyncMock(
                return_value=[_make_window("@7", "foo", "/home/u/foo")]
            )
            mock_sm.iter_thread_bindings.return_value = iter([])
            mock_sm.is_unbind_blacklisted.return_value = False
            mock_sm.get_forum_chat_id.return_value = -1001

            await sp.auto_create_topics_for_orphans(bot)

            assert sp._create_disabled_until > 0
            mock_sm.bind_thread.assert_not_called()

            # Second call within the throttle window must do nothing.
            mock_tmux.list_windows.reset_mock()
            await sp.auto_create_topics_for_orphans(bot)
            mock_tmux.list_windows.assert_not_called()
