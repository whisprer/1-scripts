#!/usr/bin/env bash
set -Eeuo pipefail

# Safely creates NEW WordPress application passwords for the ten n8n users.
# It deliberately does NOT delete or revoke any existing password.

readonly COMPOSE_DIR="/home/wofl/n8n-docker-caddy"
readonly WP_SERVICE="wordpress"
readonly WP_PATH="/var/www/html"
readonly OUTPUT_DIR="/home/wofl"
readonly WP_CLI_HOST_FALLBACK="/home/wofl/wp-cli.phar"

readonly -a ACCOUNTS=(
  "Blair Boulevard|n8n-blair|https://curiously-caffeinated.blairboulevard.website"
  "jenpark|n8n-jen|https://strategicwealthjournal.whispr.dev"
  "jordanlee|n8n-jordan|https://curiousthingsweekly.whispr.dev"
  "drkatyasteiner|n8n-katya|https://thecategoricalimperative.whispr.dev"
  "mikehenderson|n8n-mike|https://thediyblueprint.whispr.dev"
  "alexchen|n8n-alex|https://ctrlaltperspective.whispr.dev"
  "elenarodriguez|n8n-elena|https://roadslesswandered.whispr.dev"
  "rachelfoster|n8n-rachel|https://rachelfoster.whispr.dev"
  "marcodiaz|n8n-marco|https://stirringthepot.whispr.dev"
  "jakemorrison|n8n-jake|https://jakemorrison.whispr.dev"
)

WP_CONTAINER=""
OUTFILE=""

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

wp_cli() {
  sudo docker exec -e PAGER=cat -e WP_CLI_PAGER=cat "$WP_CONTAINER" \
    wp --allow-root --path="$WP_PATH" "$@"
}

ensure_wp_cli() {
  if sudo docker exec "$WP_CONTAINER" sh -lc \
      'command -v wp >/dev/null 2>&1'; then
    return 0
  fi

  [[ -f "$WP_CLI_HOST_FALLBACK" ]] \
    || die "WP-CLI is absent from the container and $WP_CLI_HOST_FALLBACK was not found."

  printf 'WP-CLI is absent from the container; restoring the previously downloaded Phar...\n'
  sudo docker cp "$WP_CLI_HOST_FALLBACK" "$WP_CONTAINER:/usr/local/bin/wp"
  sudo docker exec "$WP_CONTAINER" chmod 755 /usr/local/bin/wp
  sudo docker exec "$WP_CONTAINER" wp --info >/dev/null \
    || die "The restored WP-CLI Phar did not execute successfully."
}

csv_escape() {
  local value="$1"
  value=${value//\"/\"\"}
  printf '"%s"' "$value"
}

write_csv_row() {
  local first=1
  local value

  for value in "$@"; do
    if (( first )); then
      first=0
    else
      printf ',' >> "$OUTFILE"
    fi
    csv_escape "$value" >> "$OUTFILE"
  done
  printf '\n' >> "$OUTFILE"
}

rest_test() {
  local user="$1"
  local password="$2"
  local url="$3"

  # Passing curl's credentials through stdin keeps them out of the process list.
  {
    printf 'silent\n'
    printf 'show-error\n'
    printf 'location\n'
    printf 'connect-timeout = 15\n'
    printf 'max-time = 30\n'
    printf 'output = "/dev/null"\n'
    printf 'write-out = "%%{http_code}"\n'
    printf 'user = "%s:%s"\n' "$user" "$password"
    printf 'url = "%s/wp-json/wp/v2/users/me?context=edit"\n' "$url"
  } | curl --config -
}

printf '\nWordPress n8n application-password creator\n'
printf 'This script CREATES new passwords and NEVER revokes existing ones.\n\n'

command -v sudo >/dev/null 2>&1 || die "sudo is not installed."
command -v docker >/dev/null 2>&1 || die "docker is not installed."
command -v curl >/dev/null 2>&1 || die "curl is not installed."
command -v awk >/dev/null 2>&1 || die "awk is not installed."

[[ -d "$COMPOSE_DIR" ]] || die "Compose directory not found: $COMPOSE_DIR"

printf 'Checking sudo access...\n'
sudo -v

WP_CONTAINER=$(cd "$COMPOSE_DIR" && sudo docker compose ps -q "$WP_SERVICE")
[[ -n "$WP_CONTAINER" ]] || die "The WordPress container was not found via Docker Compose."

container_running=$(sudo docker inspect --format '{{.State.Running}}' "$WP_CONTAINER" 2>/dev/null || true)
[[ "$container_running" == "true" ]] || die "The WordPress container is not running."

ensure_wp_cli

printf 'Checking WordPress and WP-CLI in container %s...\n' "$WP_CONTAINER"
wp_cli core is-installed >/dev/null \
  || die "WordPress was not detected at $WP_PATH."

if wp_cli core is-installed --network >/dev/null 2>&1; then
  printf 'WordPress mode: multisite.\n'
else
  printf 'WordPress mode: single-site (application passwords are still supported).\n'
fi

printf 'Checking all ten WordPress users before creating anything...\n'
missing_users=()
for account in "${ACCOUNTS[@]}"; do
  IFS='|' read -r user app_label url <<< "$account"
  if ! wp_cli user get "$user" --field=ID >/dev/null 2>&1; then
    missing_users+=("$user")
  fi
done

if (( ${#missing_users[@]} > 0 )); then
  printf 'No passwords were created. Missing WordPress user(s):\n' >&2
  printf '  %s\n' "${missing_users[@]}" >&2
  exit 1
fi

wp_cli user application-password list "alexchen" --format=count >/dev/null \
  || die "This WP-CLI installation does not support WordPress application passwords."

printf 'Checking all ten WordPress REST endpoints before creating anything...\n'
unreachable_sites=()
for account in "${ACCOUNTS[@]}"; do
  IFS='|' read -r user app_label url <<< "$account"
  if ! curl --silent --show-error --fail --location \
      --connect-timeout 15 --max-time 30 --output /dev/null \
      "$url/wp-json/"; then
    unreachable_sites+=("$url")
  fi
done

if (( ${#unreachable_sites[@]} > 0 )); then
  printf 'No passwords were created. REST endpoint check failed for:\n' >&2
  printf '  %s\n' "${unreachable_sites[@]}" >&2
  exit 1
fi

printf '\nPreflight passed: container, WP-CLI, users, and REST endpoints are ready.\n'
printf 'Existing application passwords will remain valid.\n'
printf 'NOTICE: n8n-blair will belong to the network-wide Super Admin, as selected.\n'
read -r -p 'Type CREATE to make and test ten new passwords: ' confirmation
[[ "$confirmation" == "CREATE" ]] || die "Cancelled; nothing was changed."

umask 077
timestamp=$(date -u +%Y%m%dT%H%M%SZ)
OUTFILE="$OUTPUT_DIR/new-n8n-application-passwords-$timestamp.csv"
: > "$OUTFILE"
chmod 600 "$OUTFILE"
write_csv_row "user" "url" "label" "app_id" "uuid" "password" "api_test"

created_count=0
verified_count=0
failed_count=0

for account in "${ACCOUNTS[@]}"; do
  IFS='|' read -r user app_label url <<< "$account"
  label="$app_label $timestamp"
  app_id=$(cat /proc/sys/kernel/random/uuid)

  printf 'Creating a new password for %-12s ... ' "$user"
  if ! password=$(wp_cli user application-password create \
      "$user" "$label" --app-id="$app_id" --porcelain); then
    printf 'CREATE FAILED\n'
    write_csv_row "$user" "$url" "$label" "$app_id" "" "" "CREATE_FAILED"
    ((failed_count += 1))
    continue
  fi

  # WP-CLI may display application passwords in spaced groups; Basic Auth does not need them.
  password=$(printf '%s' "$password" | tr -d '[:space:]')
  if [[ -z "$password" ]]; then
    printf 'CREATE RETURNED NO PASSWORD\n'
    write_csv_row "$user" "$url" "$label" "$app_id" "" "" "EMPTY_PASSWORD"
    ((failed_count += 1))
    continue
  fi
  ((created_count += 1))

  list_csv=$(wp_cli user application-password list "$user" \
    --fields=uuid,app_id --format=csv)
  uuid=$(printf '%s\n' "$list_csv" | awk -F, -v wanted="$app_id" '
    NR > 1 {
      gsub(/^"|"$/, "", $1)
      gsub(/^"|"$/, "", $2)
      if ($2 == wanted) { print $1; exit }
    }
  ')

  http_status=""
  if ! http_status=$(rest_test "$user" "$password" "$url"); then
    http_status="CURL_ERROR"
  fi

  if [[ "$http_status" == "200" ]]; then
    api_result="PASS_200"
    ((verified_count += 1))
    printf 'created and API-tested OK\n'
  else
    api_result="FAIL_${http_status:-NO_STATUS}"
    ((failed_count += 1))
    printf 'created, but API test returned %s\n' "${http_status:-no status}"
  fi

  write_csv_row "$user" "$url" "$label" "$app_id" "$uuid" "$password" "$api_result"
done

printf '\nFinished. Created: %d; API verified: %d; problems: %d\n' \
  "$created_count" "$verified_count" "$failed_count"
printf 'Private CSV (mode 600): %s\n' "$OUTFILE"
printf 'Do not paste the CSV or its passwords into chat.\n'
printf 'No existing application password was revoked.\n'

if (( failed_count > 0 )); then
  printf 'One or more rows need attention; keep the CSV and report only the status text above.\n' >&2
  exit 2
fi
