#!/usr/bin/env bash
#
# One-shot installer for the pieces the laptop offload tool relies on.
#
# Run on the Jetson (not on the laptop):
#     sudo bash jetson/install_offload_support.sh
#
# What it does (idempotent):
#   1. DHCP on eth0 — a laptop plugged into the robot gets an address in
#      192.168.123.200-240 by itself. No gateway or DNS is handed out, so
#      the laptop keeps using its own WiFi for the internet.
#   2. Clock keeper — the Jetson has no clock battery and boots at 1970.
#      The current time is saved every minute and restored at boot, so the
#      clock has a recent lower bound (powered-off time is not recovered).
#   3. Lets record_daemon set the clock (the offload tool sends the
#      laptop's time on connect).
#   4. Reinstalls the record_daemon unit so it starts after the clock is
#      restored.
#
# Uninstall:
#     sudo systemctl disable --now go2-dhcp go2-clock-save.timer go2-clock-restore
#     sudo rm /etc/systemd/system/go2-{dhcp,clock-save,clock-restore}.service \
#             /etc/systemd/system/go2-clock-save.timer \
#             /usr/local/sbin/go2-clock-{save,restore} /etc/sudoers.d/go2-set-time
#     sudo systemctl daemon-reload

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"

if [[ $EUID -ne 0 ]]; then
    echo "This script must run as root. Re-run with: sudo bash $0" >&2
    exit 1
fi
if [[ ! -x /usr/sbin/dnsmasq ]]; then
    echo "dnsmasq not found. Install it with: sudo apt install dnsmasq-base" >&2
    exit 1
fi

echo "[1/4] DHCP for laptops on eth0..."
mkdir -p /var/lib/go2-dhcp
cat > /etc/systemd/system/go2-dhcp.service <<'EOF'
[Unit]
Description=Go2 DHCP for laptops plugged into the robot (eth0)
After=network-online.target
Wants=network-online.target

[Service]
# --port=0 turns DNS off; the empty options 3 and 6 mean "no router, no DNS".
ExecStart=/usr/sbin/dnsmasq --keep-in-foreground --port=0 \
    --interface=eth0 --bind-interfaces \
    --dhcp-range=192.168.123.200,192.168.123.240,255.255.255.0,10m \
    --dhcp-option=3 --dhcp-option=6 \
    --dhcp-leasefile=/var/lib/go2-dhcp/leases
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

echo "[2/4] Clock keeper..."
mkdir -p /var/lib/go2-clock
cat > /usr/local/sbin/go2-clock-save <<'EOF'
#!/bin/sh
# Remember the current time, unless the clock is obviously unset. The
# hardware clock is written too, because /etc/rc.local reads it at boot.
now=$(date +%s)
if [ "$now" -gt 1750000000 ]; then
    echo "$now" > /var/lib/go2-clock/last
    hwclock -w -f /dev/rtc0 2>/dev/null
fi
exit 0
EOF
cat > /usr/local/sbin/go2-clock-restore <<'EOF'
#!/bin/sh
# At boot: if the clock is behind the last saved time, jump forward to it.
f=/var/lib/go2-clock/last
[ -f "$f" ] || exit 0
saved=$(cat "$f")
[ "$saved" -gt "$(date +%s)" ] && date -s "@$saved" >/dev/null
exit 0
EOF
chmod 755 /usr/local/sbin/go2-clock-save /usr/local/sbin/go2-clock-restore

cat > /etc/systemd/system/go2-clock-restore.service <<'EOF'
[Unit]
Description=Go2 restore the clock from the last saved time
# Unitree's /etc/rc.local runs `hwclock -s`, which would undo the restore
# if it ran afterwards.
After=local-fs.target rc-local.service
Before=record_daemon.service

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/go2-clock-restore

[Install]
WantedBy=multi-user.target
EOF
cat > /etc/systemd/system/go2-clock-save.service <<'EOF'
[Unit]
Description=Go2 save the current time

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/go2-clock-save
EOF
cat > /etc/systemd/system/go2-clock-save.timer <<'EOF'
[Unit]
Description=Go2 save the current time every minute

[Timer]
OnBootSec=60
OnUnitActiveSec=60

[Install]
WantedBy=timers.target
EOF

echo "[3/4] Allowing record_daemon to set the clock..."
SUDOERS=/etc/sudoers.d/go2-set-time
echo 'unitree ALL=(root) NOPASSWD: /usr/bin/date -s @*' > "$SUDOERS.tmp"
visudo -cf "$SUDOERS.tmp" >/dev/null
chmod 440 "$SUDOERS.tmp"
mv "$SUDOERS.tmp" "$SUDOERS"

echo "[4/4] Reinstalling the record_daemon unit and starting everything..."
cp "$REPO_DIR/jetson/record_daemon.service" /etc/systemd/system/record_daemon.service
chmod 644 /etc/systemd/system/record_daemon.service
systemctl daemon-reload
systemctl enable --now go2-dhcp.service go2-clock-save.timer >/dev/null
systemctl enable go2-clock-restore.service >/dev/null
/usr/local/sbin/go2-clock-save
systemctl restart record_daemon.service

echo
systemctl is-active go2-dhcp.service go2-clock-save.timer record_daemon.service | paste -sd' '
echo "saved time: $(date -d "@$(cat /var/lib/go2-clock/last)")"
