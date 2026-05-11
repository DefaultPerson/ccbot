# ccbot — wrappers that auto-create Telegram topics for new Claude sessions.
# Naming: each function is your existing claude alias with a `b` ("bot") prefix.
#
# Install:
#   cp scripts/bashrc-ccbot.sh ~/.bashrc.d/ccbot.sh && source ~/.bashrc
#
# Requires the ccbot daemon to be running (it owns the `ccbot` tmux session).

_ccbot_spawn() {
    local cmd="$1"
    local dir="${2:-$PWD}"

    if ! tmux has-session -t ccbot 2>/dev/null; then
        echo "ccbot tmux session is not running. Start the bot first." >&2
        return 1
    fi
    if [[ ! -d "$dir" ]]; then
        echo "Not a directory: $dir" >&2
        return 1
    fi

    local wname
    wname="$(basename "$dir")"
    # `; exit` so that when Claude exits the shell exits and the tmux window
    # dies — the bot's stale-binding scan then deletes the Telegram topic.
    local wid
    wid=$(tmux new-window -d -P -F '#{window_id}' -t ccbot \
        -n "$wname" -c "$dir" "$cmd; exit")
    if [[ -z "$wid" ]]; then
        echo "Failed to create tmux window in ccbot" >&2
        return 1
    fi

    if [[ -n "$TMUX" ]]; then
        # Already inside a tmux client — switch only THIS client onto the new
        # window. Other clients of the ccbot session are not affected.
        tmux switch-client -t "ccbot:$wid"
    else
        # Free shell — create a grouped view-session so other ccbot clients
        # (e.g. /show terminals already open) don't jump to this window.
        # destroy-unattached is applied *after* attach (tmux \; chain) — if
        # set on a detached session it kills the view immediately.
        # The window-unlinked hook auto-destroys the view when THIS window
        # dies, so the terminal closes instead of falling back to __main__.
        local view="ccbot-v-${wid#@}-$(date +%s%N | tail -c 7)"
        if tmux new-session -d -s "$view" -t ccbot \; \
                select-window -t "$view:$wid" \; \
                set-hook -t "$view" window-unlinked \
                    "if-shell -F '#{==:#{hook_window},$wid}' 'kill-session -t $view'" \
                2>/dev/null; then
            tmux attach -t "$view" \; \
                set-option -t "$view" destroy-unattached on
        else
            # Fallback (shouldn't happen) — direct attach. Other clients DO
            # get yanked here; preferable to silently failing.
            tmux select-window -t "ccbot:$wid"
            tmux attach -t ccbot
        fi
    fi
}

# Main subscription
bcc()    { _ccbot_spawn 'claude' "$1"; }
bccr()   { _ccbot_spawn 'claude --resume' "$1"; }
bccd()   { _ccbot_spawn 'claude --dangerously-skip-permissions' "$1"; }
bccdr()  { _ccbot_spawn 'claude --dangerously-skip-permissions --resume' "$1"; }

# Personal subscription (CLAUDE_CONFIG_DIR=$HOME/.claude-personal)
bccp()   { _ccbot_spawn 'CLAUDE_CONFIG_DIR=$HOME/.claude-personal claude' "$1"; }
bccpr()  { _ccbot_spawn 'CLAUDE_CONFIG_DIR=$HOME/.claude-personal claude --resume' "$1"; }
bccpd()  { _ccbot_spawn 'CLAUDE_CONFIG_DIR=$HOME/.claude-personal claude --dangerously-skip-permissions' "$1"; }
bccpdr() { _ccbot_spawn 'CLAUDE_CONFIG_DIR=$HOME/.claude-personal claude --dangerously-skip-permissions --resume' "$1"; }
