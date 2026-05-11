"""Tests for /show — open a desktop terminal attached to the bound tmux window."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_update(user_id: int = 1, thread_id: int = 42) -> MagicMock:
    update = MagicMock()
    update.effective_user = MagicMock()
    update.effective_user.id = user_id
    update.message = MagicMock()
    update.message.message_thread_id = thread_id
    update.effective_chat = MagicMock()
    update.effective_chat.id = 100
    update.effective_chat.type = "supergroup"
    return update


def _make_context() -> MagicMock:
    context = MagicMock()
    context.bot = AsyncMock()
    context.user_data = {}
    return context


def _fake_proc(returncode: int | None = None) -> MagicMock:
    proc = MagicMock()
    proc.returncode = returncode
    proc.wait = AsyncMock(return_value=returncode)
    proc.stderr = None
    return proc


class TestShowCommand:
    @pytest.mark.asyncio
    async def test_show_happy_path_spawns_terminal(self):
        update = _make_update()
        context = _make_context()
        proc = _fake_proc(returncode=None)

        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot._get_thread_id", return_value=42),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.tmux_manager") as mock_tmux,
            patch("ccbot.bot.safe_reply", new_callable=AsyncMock) as mock_reply,
            patch.dict(
                "ccbot.bot.os.environ", {"WAYLAND_DISPLAY": "wayland-0"}, clear=False
            ),
            patch("ccbot.bot.shutil.which", return_value="/usr/bin/ptyxis"),
            patch(
                "ccbot.bot.asyncio.create_subprocess_exec",
                new_callable=AsyncMock,
                return_value=proc,
            ) as mock_spawn,
            patch("ccbot.bot.config") as mock_config,
        ):
            mock_config.tmux_session_name = "ccbot"
            mock_config.terminal_cmd = [
                "ptyxis",
                "--new-window",
                "--",
                "tmux",
                "attach",
                "-t",
                "{session}",
            ]
            mock_sm.resolve_window_for_thread.return_value = "@5"
            mock_sm.get_display_name.return_value = "project"
            window = MagicMock()
            window.window_id = "@5"
            mock_tmux.find_window_by_id = AsyncMock(return_value=window)
            mock_tmux.select_window = AsyncMock(return_value=True)

            from ccbot.bot import show_command

            await show_command(update, context)

            mock_tmux.select_window.assert_awaited_once_with("@5")
            args, kwargs = mock_spawn.call_args
            assert args == (
                "ptyxis",
                "--new-window",
                "--",
                "tmux",
                "attach",
                "-t",
                "ccbot",
            )
            assert kwargs["start_new_session"] is True
            mock_reply.assert_awaited()  # success reply

    @pytest.mark.asyncio
    async def test_show_unbound_topic_replies_with_error(self):
        update = _make_update()
        context = _make_context()

        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot._get_thread_id", return_value=42),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.tmux_manager") as mock_tmux,
            patch("ccbot.bot.safe_reply", new_callable=AsyncMock) as mock_reply,
        ):
            mock_sm.resolve_window_for_thread.return_value = None

            from ccbot.bot import show_command

            await show_command(update, context)

            mock_reply.assert_awaited_once()
            mock_tmux.select_window.assert_not_called()

    @pytest.mark.asyncio
    async def test_show_window_gone_replies_with_error(self):
        update = _make_update()
        context = _make_context()

        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot._get_thread_id", return_value=42),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.tmux_manager") as mock_tmux,
            patch("ccbot.bot.safe_reply", new_callable=AsyncMock) as mock_reply,
        ):
            mock_sm.resolve_window_for_thread.return_value = "@5"
            mock_sm.get_display_name.return_value = "project"
            mock_tmux.find_window_by_id = AsyncMock(return_value=None)

            from ccbot.bot import show_command

            await show_command(update, context)

            mock_reply.assert_awaited_once()
            mock_tmux.select_window.assert_not_called()

    @pytest.mark.asyncio
    async def test_show_no_display_replies_with_error(self):
        update = _make_update()
        context = _make_context()

        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot._get_thread_id", return_value=42),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.tmux_manager") as mock_tmux,
            patch("ccbot.bot.safe_reply", new_callable=AsyncMock) as mock_reply,
            patch.dict("ccbot.bot.os.environ", {}, clear=True),
        ):
            mock_sm.resolve_window_for_thread.return_value = "@5"
            mock_sm.get_display_name.return_value = "project"
            window = MagicMock()
            window.window_id = "@5"
            mock_tmux.find_window_by_id = AsyncMock(return_value=window)

            from ccbot.bot import show_command

            await show_command(update, context)

            mock_reply.assert_awaited_once()
            assert "headless" in mock_reply.call_args.args[1].lower()
            mock_tmux.select_window.assert_not_called()

    @pytest.mark.asyncio
    async def test_show_binary_not_found_replies_with_error(self):
        update = _make_update()
        context = _make_context()

        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot._get_thread_id", return_value=42),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.tmux_manager") as mock_tmux,
            patch("ccbot.bot.safe_reply", new_callable=AsyncMock) as mock_reply,
            patch.dict("ccbot.bot.os.environ", {"DISPLAY": ":0"}, clear=False),
            patch("ccbot.bot.shutil.which", return_value=None),
            patch("ccbot.bot.config") as mock_config,
        ):
            mock_config.tmux_session_name = "ccbot"
            mock_config.terminal_cmd = ["nonexistent-term"]
            mock_sm.resolve_window_for_thread.return_value = "@5"
            mock_sm.get_display_name.return_value = "project"
            window = MagicMock()
            window.window_id = "@5"
            mock_tmux.find_window_by_id = AsyncMock(return_value=window)

            from ccbot.bot import show_command

            await show_command(update, context)

            mock_reply.assert_awaited_once()
            assert "not found" in mock_reply.call_args.args[1].lower()
            mock_tmux.select_window.assert_not_called()

    @pytest.mark.asyncio
    async def test_show_select_window_failure_replies_with_error(self):
        update = _make_update()
        context = _make_context()

        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot._get_thread_id", return_value=42),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.tmux_manager") as mock_tmux,
            patch("ccbot.bot.safe_reply", new_callable=AsyncMock) as mock_reply,
            patch.dict("ccbot.bot.os.environ", {"DISPLAY": ":0"}, clear=False),
            patch("ccbot.bot.shutil.which", return_value="/usr/bin/ptyxis"),
            patch(
                "ccbot.bot.asyncio.create_subprocess_exec",
                new_callable=AsyncMock,
            ) as mock_spawn,
            patch("ccbot.bot.config") as mock_config,
        ):
            mock_config.tmux_session_name = "ccbot"
            mock_config.terminal_cmd = ["ptyxis"]
            mock_sm.resolve_window_for_thread.return_value = "@5"
            mock_sm.get_display_name.return_value = "project"
            window = MagicMock()
            window.window_id = "@5"
            mock_tmux.find_window_by_id = AsyncMock(return_value=window)
            mock_tmux.select_window = AsyncMock(return_value=False)

            from ccbot.bot import show_command

            await show_command(update, context)

            mock_reply.assert_awaited_once()
            mock_spawn.assert_not_called()

    @pytest.mark.asyncio
    async def test_show_rejects_unauthorized_user(self):
        update = _make_update()
        context = _make_context()

        with (
            patch("ccbot.bot.is_user_allowed", return_value=False),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.tmux_manager") as mock_tmux,
            patch("ccbot.bot.safe_reply", new_callable=AsyncMock) as mock_reply,
        ):
            from ccbot.bot import show_command

            await show_command(update, context)

            mock_reply.assert_not_called()
            mock_tmux.select_window.assert_not_called()
            mock_sm.resolve_window_for_thread.assert_not_called()

    @pytest.mark.asyncio
    async def test_show_substitutes_window_id_placeholder(self):
        update = _make_update()
        context = _make_context()
        proc = _fake_proc(returncode=None)

        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot._get_thread_id", return_value=42),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.tmux_manager") as mock_tmux,
            patch("ccbot.bot.safe_reply", new_callable=AsyncMock),
            patch.dict("ccbot.bot.os.environ", {"DISPLAY": ":0"}, clear=False),
            patch("ccbot.bot.shutil.which", return_value="/usr/bin/alacritty"),
            patch(
                "ccbot.bot.asyncio.create_subprocess_exec",
                new_callable=AsyncMock,
                return_value=proc,
            ) as mock_spawn,
            patch("ccbot.bot.config") as mock_config,
        ):
            mock_config.tmux_session_name = "ccbot"
            mock_config.terminal_cmd = [
                "alacritty",
                "-e",
                "tmux",
                "select-window",
                "-t",
                "{session}:{window_id}",
            ]
            mock_sm.resolve_window_for_thread.return_value = "@7"
            mock_sm.get_display_name.return_value = "p"
            window = MagicMock()
            window.window_id = "@7"
            mock_tmux.find_window_by_id = AsyncMock(return_value=window)
            mock_tmux.select_window = AsyncMock(return_value=True)

            from ccbot.bot import show_command

            await show_command(update, context)

            args, _ = mock_spawn.call_args
            assert args == (
                "alacritty",
                "-e",
                "tmux",
                "select-window",
                "-t",
                "ccbot:@7",
            )
