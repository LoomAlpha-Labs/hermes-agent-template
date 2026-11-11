#!/usr/bin/env bash
# Runs check.sh; on failure posts the violation lines to Telegram.
# Needs TELEGRAM_BOT_TOKEN and HERMES_ALERT_CHAT_ID in /etc/hermes/hermes.env
# (HERMES_ALERT_CHAT_ID must be added there by hand). Never prints the token.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE=/etc/hermes/hermes.env

out="$("$HERE/check.sh" 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && exit 0
echo "$out"   # to journal; contains no secrets

# Read two keys without sourcing the file (it may contain arbitrary values).
getvar() { grep -E "^$1=" "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2- | sed -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'\$//"; }
token="$(getvar TELEGRAM_BOT_TOKEN)"
chat="$(getvar HERMES_ALERT_CHAT_ID)"
if [ -z "$token" ] || [ -z "$chat" ]; then
  echo "alert not sent: TELEGRAM_BOT_TOKEN or HERMES_ALERT_CHAT_ID missing in $ENV_FILE" >&2
  exit "$rc"
fi

# printf is a shell builtin and curl reads its config from stdin, so the
# token never appears in any process argument list.
code="$(printf 'url = "https://api.telegram.org/bot%s/sendMessage"\n' "$token" |
  curl -sS -o /dev/null -w '%{http_code}' --max-time 20 --config - \
    --data-urlencode "chat_id=$chat" \
    --data-urlencode "text=hermes host check failed on $(hostname):
$out" 2>/dev/null)"
case "$code" in
  2??) ;;
  *) echo "alert delivery failed (HTTP status ${code:-none})" >&2 ;;
esac
exit "$rc"
