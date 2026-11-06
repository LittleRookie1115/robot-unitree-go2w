#!/usr/bin/env bash
# Install the user-level VLM router unit and its default configuration.
# The unit is deliberately not enabled: robonix_start starts it with the stack.
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
directory="$HOME/.config/systemd/user"
config_dir="$HOME/.config/robonix-vlm-router"
mkdir -p "$directory" "$config_dir"

if [[ ! -f "$config_dir/config.json" ]]; then
  cat > "$config_dir/config.json" <<EOF
{
  "listen_host": "127.0.0.1",
  "listen_port": 16667,
  "state_file": "$HOME/.config/robonix-vlm-router/state.json",
  "context_window_tokens": 8192,
  "local_max_tokens": 1024,
  "connect_timeout_seconds": 10,
  "read_timeout_seconds": 300,
  "external": {
    "base_url": "http://127.0.0.1:16668/v1",
    "health_url": "http://127.0.0.1:16668/health",
    "model": "gpt-6.1-sol"
  },
  "local": {
    "base_url": "http://127.0.0.1:11435/v1",
    "health_url": "http://127.0.0.1:11435/api/version",
    "model": "go2w-qwen2.5-1.5b-q4km-plan10"
  }
}
EOF
  chmod 600 "$config_dir/config.json"
fi

cat > "$directory/robonix-vlm-router.service" <<EOF
[Unit]
Description=Robonix loopback VLM router (external API / local model)
After=network.target
Before=robonix-go2-native.service

[Service]
Type=simple
WorkingDirectory=$root/native
Environment=PYTHONDONTWRITEBYTECODE=1
Environment=ROBONIX_VLM_ROUTER_CONFIG=$config_dir/config.json
ExecStart=/bin/bash $root/native/start-vlm-router.sh
Restart=on-failure
RestartSec=2
KillSignal=SIGTERM
KillMode=mixed
TimeoutStopSec=10
NoNewPrivileges=yes
PrivateTmp=yes

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
echo "VLM router unit installed. Start with: robonix_start"
echo "Config: $config_dir/config.json"
