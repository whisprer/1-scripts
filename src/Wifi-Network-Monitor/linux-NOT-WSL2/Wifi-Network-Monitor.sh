#!/usr/bin/env bash

set -u

readonly VENDOR_API="https://api.macvendors.com"
readonly CURL_TIMEOUT_SECONDS=10

declare -A VENDOR_CACHE=()

lookup_vendor() {
  local mac="$1"
  local oui vendor

  # The vendor API needs the first three octets of the MAC address.
  oui="${mac:0:8}"

  if [[ -n "${VENDOR_CACHE[$oui]+present}" ]]; then
    LOOKUP_VENDOR_RESULT="${VENDOR_CACHE[$oui]}"
    return
  fi

  if vendor="$(curl --fail --silent --show-error \
      --max-time "$CURL_TIMEOUT_SECONDS" \
      "${VENDOR_API}/${oui}:" 2>/dev/null)" && [[ -n "$vendor" ]]; then
    # Keep every device on one report line even if a response contains newlines.
    vendor="${vendor//$'\r'/ }"
    vendor="${vendor//$'\n'/ }"
  else
    vendor="Unknown (lookup failed)"
  fi

  VENDOR_CACHE["$oui"]="$vendor"
  LOOKUP_VENDOR_RESULT="$vendor"
}

mapfile -t devices < <(
  arp -a 2>/dev/null \
    | awk 'tolower($0) !~ /incomplete/ {gsub(/[()]/, "", $2); print $2, tolower($4)}'
)

echo "========================================================================"
echo "  Devices currently on your network"
echo "  $(date)"
echo "========================================================================"
printf "%-18s %-20s %s\n" "IP Address" "MAC Address" "Vendor"
echo "------------------------------------------------------------------------"

device_count=0
for device in "${devices[@]}"; do
  read -r ip mac <<< "$device"

  # Ignore malformed ARP output rather than sending it to the remote API.
  if [[ ! "$mac" =~ ^([[:xdigit:]]{2}:){5}[[:xdigit:]]{2}$ ]]; then
    continue
  fi

  lookup_vendor "$mac"
  vendor="$LOOKUP_VENDOR_RESULT"
  printf "%-18s %-20s %s\n" "$ip" "$mac" "$vendor"
  ((device_count += 1))
done

echo "========================================================================"
echo "Total: ${device_count} device(s) found"

# To run every five minutes, add this line with `crontab -e` (do not put it
# directly inside this script), replacing the path with this script's location:
# */5 * * * * /path/to/wifi-scan.sh >> /var/log/wifi-monitor.log 2>&1
