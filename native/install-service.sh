#!/usr/bin/env bash
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
directory="$HOME/.config/systemd/user"
mkdir -p "$directory"
cat > "$directory/robonix-go2-native.service" <<EOF
[Unit]
Description=Native Go2 Robonix deployment on Ubuntu 20.04 and ROS 2 Foxy
Conflicts=robonix-core.service
After=robonix-core.service

[Service]
Type=simple
WorkingDirectory=$root/native
ExecStart=/bin/bash $root/native/start.sh
KillSignal=SIGINT
KillMode=mixed
TimeoutStopSec=45
Restart=no

[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload
echo "User service installed. Start with: systemctl --user start robonix-go2-native"
