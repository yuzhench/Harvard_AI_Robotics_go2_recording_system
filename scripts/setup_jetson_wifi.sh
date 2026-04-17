#!/usr/bin/env bash
#
# One-shot Jetson WiFi setup for the Go2 record pipeline.
#
# What this does (idempotent, safe to re-run):
#   1. Creates a NetworkManager profile for phswifi3 (WPA2-Enterprise / 802.1X PEAP+MSCHAPv2)
#      with autoconnect enabled, so the Jetson reconnects on every boot as soon as
#      the USB WiFi adapter comes up and phswifi3 is in range.
#   2. Tells the eth0 profile (wired link to Go2) to never install an IPv4 default
#      route, so public-internet traffic always goes out over wlan0 while the
#      192.168.123.0/24 DDS traffic still flows through eth0.
#
# Credentials are prompted interactively (never stored in this file or in shell
# history). NetworkManager writes them to
#   /etc/NetworkManager/system-connections/phswifi3.nmconnection
# with mode 600, readable only by root.
#
# Usage (on the Jetson, after rsyncing this repo):
#   sudo bash scripts/setup_jetson_wifi.sh
#

set -euo pipefail

SSID="phswifi3"
EAP_METHOD="peap"
PHASE2_AUTH="mschapv2"
AUTOCONNECT_PRIORITY=10   # higher than default (0) so phswifi3 wins over
                          # the iPhone hotspot profile when both are in range

if [[ $EUID -ne 0 ]]; then
  echo "This script must run as root. Re-run with: sudo bash $0" >&2
  exit 1
fi

if ! command -v nmcli >/dev/null 2>&1; then
  echo "nmcli not found. Install network-manager first." >&2
  exit 1
fi

if ! ip link show wlan0 >/dev/null 2>&1; then
  echo "wlan0 not found. Is the USB WiFi adapter plugged in?" >&2
  exit 1
fi

echo "=== Jetson WiFi setup for '${SSID}' ==="
echo

# --- 1. Collect credentials without echoing or saving to history --------------
read -rp "phswifi3 username: " PHS_USER
if [[ -z "$PHS_USER" ]]; then
  echo "Username cannot be empty." >&2
  exit 1
fi

read -rsp "phswifi3 password: " PHS_PASS
echo
if [[ -z "$PHS_PASS" ]]; then
  echo "Password cannot be empty." >&2
  exit 1
fi

# --- 2. Recreate profile (idempotent) ----------------------------------------
if nmcli -t -f NAME connection show | grep -qx "$SSID"; then
  echo "Existing '${SSID}' profile found — deleting and recreating..."
  nmcli connection delete "$SSID" >/dev/null
fi

echo "Creating '${SSID}' profile..."
nmcli connection add type wifi ifname wlan0 \
  con-name "$SSID" \
  ssid "$SSID" \
  wifi-sec.key-mgmt wpa-eap \
  802-1x.eap "$EAP_METHOD" \
  802-1x.phase2-auth "$PHASE2_AUTH" \
  802-1x.identity "$PHS_USER" \
  802-1x.password "$PHS_PASS" \
  802-1x.system-ca-certs no \
  connection.autoconnect yes \
  connection.autoconnect-priority "$AUTOCONNECT_PRIORITY" >/dev/null

# Clear the password variable from script memory as soon as possible.
unset PHS_PASS

# --- 3. Prevent eth0 from hijacking the default route ------------------------
ETH_CON=$(nmcli -t -f NAME,DEVICE connection show | awk -F: '$2=="eth0"{print $1; exit}')
if [[ -n "$ETH_CON" ]]; then
  CURRENT=$(nmcli -g ipv4.never-default connection show "$ETH_CON" || echo "")
  if [[ "$CURRENT" != "yes" ]]; then
    echo "Setting 'ipv4.never-default=yes' on '${ETH_CON}'..."
    nmcli connection modify "$ETH_CON" ipv4.never-default yes
    if nmcli -t -f NAME,DEVICE connection show --active | grep -q "^${ETH_CON}:eth0$"; then
      echo "Reactivating '${ETH_CON}' to apply the change..."
      nmcli connection down "$ETH_CON" >/dev/null || true
      nmcli connection up   "$ETH_CON" >/dev/null || true
    fi
  else
    echo "'${ETH_CON}' already has ipv4.never-default=yes — skipping."
  fi
else
  echo "No eth0 profile found — skipping route fix (safe to run again later)."
fi

# --- 4. Report -----------------------------------------------------------------
echo
echo "=== Done ==="
echo "Profile '${SSID}' is saved with autoconnect=yes, priority=${AUTOCONNECT_PRIORITY}."
echo "It will reconnect automatically every time the Jetson boots and the AP is in range."
echo
echo "To activate right now:"
echo "    sudo nmcli connection up ${SSID}"
echo
echo "To verify later:"
echo "    nmcli connection show --active"
echo "    ip route"
echo "    ping -c 3 8.8.8.8"
