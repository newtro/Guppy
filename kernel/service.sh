#!/usr/bin/env bash
# Run Guppy's kernel as a per-user launchd service (starts at login, restarts if it dies).
#   kernel/service.sh install | uninstall | restart | status | logs
# Per-user (LaunchAgent), not system-wide: Guppy needs the Admiral's Keychain and CLI logins (claude, codex, grok, az).
set -euo pipefail

LABEL="ai.guppy.kernel"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOGS="$HOME/Library/Logs/Guppy"
DOMAIN="gui/$(id -u)"
SERVICE_PATH="$HOME/.local/bin:$HOME/.grok/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

write_plist() {
  mkdir -p "$LOGS" "$(dirname "$PLIST")"
  cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$ROOT/.venv/bin/python</string><string>-u</string><string>-m</string><string>kernel.voice.app</string>
  </array>
  <key>WorkingDirectory</key><string>$ROOT</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>$SERVICE_PATH</string>
    <key>HOME</key><string>$HOME</string>
    <key>PYTHONUNBUFFERED</key><string>1</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>ProcessType</key><string>Interactive</string>
  <key>StandardOutPath</key><string>$LOGS/kernel.log</string>
  <key>StandardErrorPath</key><string>$LOGS/kernel.log</string>
</dict>
</plist>
EOF
  plutil -lint "$PLIST" >/dev/null
}

case "${1:-status}" in
  install)
    if lsof -tiTCP:8765 -sTCP:LISTEN >/dev/null 2>&1 && ! launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
      echo "Port 8765 is in use by a Guppy started by hand; stop it first." >&2; exit 1
    fi
    write_plist
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    launchctl bootstrap "$DOMAIN" "$PLIST"
    echo "Installed $LABEL. Logs: $LOGS/kernel.log  UI: http://127.0.0.1:8765"
    ;;
  uninstall)
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    rm -f "$PLIST"
    echo "Removed $LABEL."
    ;;
  restart)
    launchctl kickstart -k "$DOMAIN/$LABEL"
    ;;
  status)
    launchctl print "$DOMAIN/$LABEL" 2>/dev/null | grep -E "^\s+(state|pid|last exit code|runs) " || echo "$LABEL is not installed."
    curl -s -m 2 -o /dev/null -w "http %{http_code}\n" http://127.0.0.1:8765/api/tasks?limit=1 || true
    ;;
  logs)
    tail -n "${2:-50}" -f "$LOGS/kernel.log"
    ;;
  *)
    echo "usage: $0 install|uninstall|restart|status|logs" >&2; exit 2
    ;;
esac
