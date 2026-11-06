#!/usr/bin/env bash
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
directory="$HOME/.config/systemd/user"
mkdir -p "$directory"
cat > "$directory/robonix-go2w-motion.service" <<EOF
[Unit]
Description=Explicit Go2-W native manual motion session, disarmed at start
Conflicts=robonix-go2-native.service robonix-core.service
After=robonix-go2-native.service robonix-core.service

[Service]
Type=simple
WorkingDirectory=$root
ExecStart=/bin/bash $root/native/motion-session.sh --operator-present
KillSignal=SIGINT
KillMode=mixed
TimeoutStopSec=30
Restart=no
EOF
systemctl --user daemon-reload
