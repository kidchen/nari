#!/usr/bin/env bash
# nari-watchdog — runs on the VM (systemd timer, every 5 min). Alerts the admin on Telegram when the
# bot container is down or unhealthy, because a dead bot can't report itself. Alerts once on failure
# (after 2 consecutive bad checks, so deploy restarts don't page) and once on recovery.
# Usage: nari-watchdog /home/<user>/nari/.env
set -u
ENV_FILE="${1:?usage: nari-watchdog <path to nari .env>}"
STATE_DIR=/var/lib/nari-watchdog
CHAT_FILE=/var/lib/docker/volumes/nari-data/_data/admin_chat_id   # written by the bot at startup
mkdir -p "$STATE_DIR"

status=$(docker inspect -f '{{.State.Status}}/{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' nari 2>/dev/null || echo "missing/none")
case "$status" in
  running/healthy) bad=0 ;;
  running/starting) exit 0 ;;                 # just (re)started: decide on the next run
  *) bad=1 ;;
esac

send() {
  local token chat
  token=$(grep -E '^TELEGRAM_BOT_TOKEN=' "$ENV_FILE" | head -1 | cut -d= -f2-)
  chat=$(cat "$CHAT_FILE" 2>/dev/null)
  [ -n "$token" ] && [ -n "$chat" ] || { echo "watchdog: no token/chat to alert"; return 1; }
  # Token goes to curl via stdin (-K -), never on the command line / process list.
  printf 'url = "https://api.telegram.org/bot%s/sendMessage"\n' "$token" |
    curl -sS -m 20 -o /dev/null -K - --data-urlencode "chat_id=$chat" --data-urlencode "text=$1"
}

count=$(cat "$STATE_DIR/bad_count" 2>/dev/null || echo 0)
alerted=$([ -f "$STATE_DIR/alerted" ] && echo 1 || echo 0)
if [ "$bad" = 1 ]; then
  count=$((count + 1)); echo "$count" > "$STATE_DIR/bad_count"
  if [ "$count" -ge 2 ] && [ "$alerted" = 0 ]; then
    send "🚨 Nari bot is DOWN on $(hostname) (container: $status). Check: cd ~/nari && docker compose logs --tail 50" \
      && touch "$STATE_DIR/alerted"
  fi
else
  echo 0 > "$STATE_DIR/bad_count"
  if [ "$alerted" = 1 ]; then
    send "✅ Nari bot is back (container: $status)" && rm -f "$STATE_DIR/alerted"
  fi
fi
