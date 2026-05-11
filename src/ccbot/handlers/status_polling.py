"""Terminal status line polling for thread-bound windows.

Provides background polling of terminal status lines for all active users:
  - Detects Claude Code status (working, waiting, etc.)
  - Detects interactive UIs (permission prompts) not triggered via JSONL
  - Updates status messages in Telegram
  - Polls thread_bindings (each topic = one window)
  - Periodically probes topic existence via unpin_all_forum_topic_messages
    (silent no-op when no pins); cleans up deleted topics (kills tmux window
    + unbinds thread)
  - Periodically scans tmux for unbound windows and auto-creates topics for
    them (so externally-started Claude sessions appear in Telegram).

Key components:
  - STATUS_POLL_INTERVAL: Polling frequency (1 second)
  - TOPIC_CHECK_INTERVAL: Topic existence probe frequency (60 seconds)
  - status_poll_loop: Background polling task
  - update_status_message: Poll and enqueue status updates
  - auto_create_topics_for_orphans: Periodic orphan-window scan
"""

import asyncio
import logging
import time
from pathlib import Path

from telegram import Bot
from telegram.error import BadRequest, Forbidden, TelegramError

from ..config import config
from ..session import session_manager
from ..terminal_parser import is_interactive_ui, parse_status_line
from ..tmux_manager import tmux_manager
from .interactive_ui import (
    clear_interactive_msg,
    get_interactive_window,
    handle_interactive_ui,
)
from .cleanup import clear_topic_state
from .message_queue import enqueue_status_update, get_message_queue

logger = logging.getLogger(__name__)

# Status polling interval
STATUS_POLL_INTERVAL = 1.0  # seconds - faster response (rate limiting at send layer)

# Topic existence probe interval (silent supergroup path).
TOPIC_CHECK_INTERVAL = 5.0  # seconds

# Private-chat probe sends an invisible message and deletes it immediately —
# the only Bot API that actually returns "Message thread not found" in private
# chats. Slightly noisier, so run less often.
PRIVATE_TOPIC_CHECK_INTERVAL = 60.0  # seconds

# Invisible zero-width character used as the body of private-chat probes.
_PROBE_PAYLOAD = "​"

# Auto-create orphan-topic scan: throttle to 1h after permission failure.
_PERM_THROTTLE_SECONDS = 3600.0

# Shell names that mean "claude not started yet" — fall back to cwd for the topic name.
_GENERIC_SHELL_NAMES = frozenset(
    {"bash", "zsh", "sh", "fish", "dash", "ksh", "node", "claude"}
)

# Substrings in BadRequest messages that indicate the topic is gone
_TOPIC_GONE_MARKERS = ("Topic_id_invalid", "Message thread not found")

# Module state for auto-create scan
_creating_topics: set[str] = set()
_create_disabled_until: float = 0.0


async def update_status_message(
    bot: Bot,
    user_id: int,
    window_id: str,
    thread_id: int | None = None,
    skip_status: bool = False,
) -> None:
    """Poll terminal and check for interactive UIs and status updates.

    UI detection always happens regardless of skip_status. When skip_status=True,
    only UI detection runs (used when message queue is non-empty to avoid
    flooding the queue with status updates).

    Also detects permission prompt UIs (not triggered via JSONL) and enters
    interactive mode when found.
    """
    w = await tmux_manager.find_window_by_id(window_id)
    if not w:
        # Window gone, enqueue clear (unless skipping status)
        if not skip_status:
            await enqueue_status_update(
                bot, user_id, window_id, None, thread_id=thread_id
            )
        return

    pane_text = await tmux_manager.capture_pane(w.window_id)
    if not pane_text:
        # Transient capture failure - keep existing status message
        return

    interactive_window = get_interactive_window(user_id, thread_id)
    should_check_new_ui = True

    if interactive_window == window_id:
        # User is in interactive mode for THIS window
        if is_interactive_ui(pane_text):
            # Interactive UI still showing — skip status update (user is interacting)
            return
        # Interactive UI gone — clear interactive mode, fall through to status check.
        # Don't re-check for new UI this cycle (the old one just disappeared).
        await clear_interactive_msg(user_id, bot, thread_id)
        should_check_new_ui = False
    elif interactive_window is not None:
        # User is in interactive mode for a DIFFERENT window (window switched)
        # Clear stale interactive mode
        await clear_interactive_msg(user_id, bot, thread_id)

    # Check for permission prompt (interactive UI not triggered via JSONL)
    # ALWAYS check UI, regardless of skip_status
    if should_check_new_ui and is_interactive_ui(pane_text):
        logger.debug(
            "Interactive UI detected in polling (user=%d, window=%s, thread=%s)",
            user_id,
            window_id,
            thread_id,
        )
        await handle_interactive_ui(bot, user_id, window_id, thread_id)
        return

    # Normal status line check — skip if queue is non-empty
    if skip_status:
        return

    status_line = parse_status_line(pane_text)

    if status_line:
        await enqueue_status_update(
            bot,
            user_id,
            window_id,
            status_line,
            thread_id=thread_id,
        )
    # If no status line, keep existing status message (don't clear on transient state)


def _pick_topic_name(window_name: str, cwd: str, window_id: str) -> str:
    """Choose a human-friendly topic name for an orphan tmux window."""
    name = (window_name or "").strip()
    if name and name.lower() not in _GENERIC_SHELL_NAMES:
        return name[:128]
    cwd = (cwd or "").strip()
    if cwd:
        base = Path(cwd).name
        if base:
            return base[:128]
    return f"tmux-{window_id}"


async def auto_create_topics_for_orphans(bot: Bot) -> None:
    """Scan ccbot tmux session for windows without a thread binding and create
    Telegram topics for them.

    Designed to run on a timer from status_poll_loop. No-op when disabled via
    `CCBOT_AUTO_CREATE_TOPICS=false`, throttled for 1h after a permission error.
    """
    global _create_disabled_until

    if not config.auto_create_topics:
        return
    now = time.monotonic()
    if now < _create_disabled_until:
        return

    try:
        windows = await tmux_manager.list_windows()
    except Exception as e:
        logger.debug("auto_create scan: list_windows failed: %s", e)
        return
    if not windows:
        return

    bound_ids = {wid for _, _, wid in session_manager.iter_thread_bindings()}
    orphans = [
        w
        for w in windows
        if w.window_id
        and w.window_id not in bound_ids
        and w.window_id not in _creating_topics
        and not session_manager.is_unbind_blacklisted(w.window_id)
    ]
    if not orphans:
        return

    # Pick the owner: first allowed user that has a known forum chat_id.
    owner_id: int | None = None
    chat_id: int | None = None
    for candidate in sorted(config.allowed_users):
        c = session_manager.get_forum_chat_id(candidate)
        if c is not None:
            owner_id = candidate
            chat_id = c
            break
    # Fallback to explicit CCBOT_FORUM_CHAT_ID — lets a fresh install bootstrap
    # without requiring the user to send a first message into a topic.
    if chat_id is None and config.forum_chat_id is not None:
        chat_id = config.forum_chat_id
        owner_id = sorted(config.allowed_users)[0] if config.allowed_users else None
    if owner_id is None or chat_id is None:
        logger.debug(
            "auto_create scan: no forum chat_id known (set CCBOT_FORUM_CHAT_ID or "
            "post a message into a forum topic once); skipping"
        )
        return

    for w in orphans:
        wid = w.window_id
        _creating_topics.add(wid)
        try:
            name = _pick_topic_name(w.window_name, w.cwd, wid)
            try:
                topic = await bot.create_forum_topic(chat_id=chat_id, name=name)
            except Forbidden as e:
                _create_disabled_until = time.monotonic() + _PERM_THROTTLE_SECONDS
                logger.error(
                    "create_forum_topic forbidden (auto-create disabled 1h): %s", e
                )
                return
            except BadRequest as e:
                msg = str(e).lower()
                if "not enough rights" in msg or "rights_to_manage" in msg:
                    _create_disabled_until = time.monotonic() + _PERM_THROTTLE_SECONDS
                    logger.error(
                        "Bot lacks can_manage_topics (auto-create disabled 1h): %s",
                        e,
                    )
                    return
                logger.error("create_forum_topic failed for %s: %s", wid, e)
                continue
            except TelegramError as e:
                logger.error("create_forum_topic error for %s: %s", wid, e)
                continue

            thread_id = topic.message_thread_id
            session_manager.bind_thread(owner_id, thread_id, wid, window_name=name)
            session_manager.set_group_chat_id(owner_id, thread_id, chat_id)

            if name != w.window_name:
                await tmux_manager.rename_window(wid, name)
                session_manager.update_display_name(wid, name)
            await tmux_manager.set_window_option(wid, "allow-rename", "off")

            logger.info(
                "Auto-created topic '%s' (thread=%d) for orphan window %s",
                name,
                thread_id,
                wid,
            )
            try:
                await bot.send_message(
                    chat_id=chat_id,
                    message_thread_id=thread_id,
                    text=f"📎 Auto-bound to tmux window '{name}' ({wid}).",
                )
            except TelegramError as e:
                logger.debug("welcome message failed for %s: %s", wid, e)
        finally:
            _creating_topics.discard(wid)


async def _probe_topic_alive(bot: Bot, chat_id: int, thread_id: int) -> bool:
    """Detect whether a forum topic still exists. Returns False iff Telegram
    indicates the thread is gone (TOPIC_GONE_MARKERS).

    - Supergroups: `unpin_all_forum_topic_messages` (silent no-op when no pins).
    - Private chats: send an invisible message and delete it immediately. This
      is the only Bot API call that actually returns "Message thread not found"
      in private chats — `unpin_all_forum_topic_messages` and `send_chat_action`
      both return ok=true regardless of whether the thread exists.
    """
    # Negative chat_id == supergroup/channel. user_id is always positive.
    is_supergroup = chat_id < 0
    try:
        if is_supergroup:
            await bot.unpin_all_forum_topic_messages(
                chat_id=chat_id, message_thread_id=thread_id
            )
            return True
        # Private chat — send + delete probe.
        msg = await bot.send_message(
            chat_id=chat_id,
            text=_PROBE_PAYLOAD,
            message_thread_id=thread_id,
            disable_notification=True,
        )
        try:
            await bot.delete_message(chat_id=chat_id, message_id=msg.message_id)
        except TelegramError as e:
            logger.debug(
                "Probe delete failed for thread=%d msg=%d: %s",
                thread_id,
                msg.message_id,
                e,
            )
        return True
    except BadRequest as e:
        if any(m in str(e) for m in _TOPIC_GONE_MARKERS):
            return False
        logger.debug("Topic probe BadRequest for thread=%d: %s", thread_id, e)
        return True
    except TelegramError as e:
        logger.debug("Topic probe error for thread=%d: %s", thread_id, e)
        return True


async def cleanup_dead_window_binding(
    bot: Bot, user_id: int, thread_id: int, window_id: str
) -> None:
    """Tear down all state for a tmux window that no longer exists and delete
    the bound Telegram topic.

    Used by status_poll_loop when find_window_by_id returns None — covers the
    case where the user closed Claude (`; exit` collapses the window) or killed
    the window directly. Symmetric to topic-deletion → window-kill.
    """
    chat_id = session_manager.resolve_chat_id(user_id, thread_id)
    session_manager.unbind_thread(user_id, thread_id)
    session_manager.purge_window(window_id)
    await session_manager.remove_session_map_entry(window_id)
    await clear_topic_state(user_id, thread_id, bot)
    try:
        await bot.delete_forum_topic(chat_id=chat_id, message_thread_id=thread_id)
        logger.info(
            "Window gone — deleted topic thread=%d for user=%d (window_id=%s)",
            thread_id,
            user_id,
            window_id,
        )
    except BadRequest as e:
        if not any(m in str(e) for m in _TOPIC_GONE_MARKERS):
            logger.warning("delete_forum_topic failed for thread=%d: %s", thread_id, e)
    except TelegramError as e:
        logger.warning("delete_forum_topic error for thread=%d: %s", thread_id, e)


async def status_poll_loop(bot: Bot) -> None:
    """Background task to poll terminal status for all thread-bound windows."""
    logger.info("Status polling started (interval: %ss)", STATUS_POLL_INTERVAL)
    last_supergroup_probe = 0.0
    last_private_probe = 0.0
    last_auto_create = 0.0
    while True:
        try:
            # Periodic auto-create orphan-topic scan
            now = time.monotonic()
            if now - last_auto_create >= config.auto_create_topics_interval:
                last_auto_create = now
                try:
                    await auto_create_topics_for_orphans(bot)
                except Exception as e:  # noqa: BLE001
                    logger.error("auto_create_topics_for_orphans failed: %s", e)

            # Periodic topic-existence probe. Supergroups use a silent API and
            # can run frequently; private chats fall back to send_chat_action
            # (typing indicator) so we slow them down.
            run_supergroup = now - last_supergroup_probe >= TOPIC_CHECK_INTERVAL
            run_private = now - last_private_probe >= PRIVATE_TOPIC_CHECK_INTERVAL
            if run_supergroup or run_private:
                if run_supergroup:
                    last_supergroup_probe = now
                if run_private:
                    last_private_probe = now
                for user_id, thread_id, wid in list(
                    session_manager.iter_thread_bindings()
                ):
                    chat_id = session_manager.resolve_chat_id(user_id, thread_id)
                    is_private = chat_id == user_id
                    if is_private and not run_private:
                        continue
                    if (not is_private) and not run_supergroup:
                        continue
                    alive = await _probe_topic_alive(bot, chat_id, thread_id)
                    if not alive:
                        w = await tmux_manager.find_window_by_id(wid)
                        if w:
                            await tmux_manager.kill_window(w.window_id)
                        await cleanup_dead_window_binding(bot, user_id, thread_id, wid)
                        logger.info(
                            "Topic gone: killed window %s and unbound thread %d "
                            "for user %d",
                            wid,
                            thread_id,
                            user_id,
                        )

            for user_id, thread_id, wid in list(session_manager.iter_thread_bindings()):
                try:
                    # Window gone (user exited Claude, killed the window, etc.)
                    # → tear down state and delete the bound Telegram topic.
                    w = await tmux_manager.find_window_by_id(wid)
                    if not w:
                        await cleanup_dead_window_binding(bot, user_id, thread_id, wid)
                        continue

                    # UI detection happens unconditionally in update_status_message.
                    # Status enqueue is skipped inside update_status_message when
                    # interactive UI is detected (returns early) or when queue is non-empty.
                    queue = get_message_queue(user_id)
                    skip_status = queue is not None and not queue.empty()

                    await update_status_message(
                        bot,
                        user_id,
                        wid,
                        thread_id=thread_id,
                        skip_status=skip_status,
                    )
                except Exception as e:
                    logger.debug(
                        f"Status update error for user {user_id} "
                        f"thread {thread_id}: {e}"
                    )
        except Exception as e:
            logger.error(f"Status poll loop error: {e}")

        await asyncio.sleep(STATUS_POLL_INTERVAL)
