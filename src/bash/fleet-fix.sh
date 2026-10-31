#!/usr/bin/env bash
# fleet-fix.sh - apply the fixes proven on LON1 (Oct 2026) to any of Wofl's servers.
# It works out what THIS server needs, shows the plan, and changes nothing until you
# type 'yes'. Safe to run again: anything already right is left alone.
#
#   sudo bash fleet-fix.sh
#
# Fixes it can make (only where needed):
#   login notice   list failed services at every SSH login
#   rkhunter       nightly 02:40, never at boot; fix Ubuntu's WEB_CMD bug; refresh
#                  fingerprints after package installs
#   chkrootkit     nightly 02:20, findings logged (no false alarms at login)
#   apt            retire broken custom apt jobs; make sure security updates install
#   ClamAV         no boot-time runs; on boxes under 2 GB RAM: scan only writable
#                  folders, lowest priority, scan dies first if memory runs out
#   fail2ban       guard the REAL ssh port; recidive; dovecot / postfix-sasl jails
#                  where mail runs
#   certbot        delete expired certificates nothing uses; stop certbot's timer where
#                  Caddy already handles certificates
#   ports          close the beacon dashboard ports (8080/8081/8083) to the internet;
#                  where Docker runs, keep them open to Docker's own networks
#   SSH            keys only (done last; your current session stays connected)
# Reports only (no changes): Docker ports open to the world, root's SSH keys, disk use.
# Every file it touches is copied to /root/fleet-fix-backup-<date-time>/ first.
set -uo pipefail
export LC_ALL=C

# --- paths (overridable for testing only) ----------------------------------
SD="${SYSTEMD_DIR:-/etc/systemd/system}"
MOTD_DIR="${MOTD_DIR:-/etc/update-motd.d}"
F2B="${F2B_DIR:-/etc/fail2ban}"
SSHD_DIR="${SSHD_DIR:-/etc/ssh}"
APTD="${APT_CONF_DIR:-/etc/apt/apt.conf.d}"
DEFD="${DEFAULT_DIR:-/etc/default}"
RKCONF="${RKHUNTER_CONF:-/etc/rkhunter.conf}"
RKLOG="${RKHUNTER_LOG:-/var/log/rkhunter.log}"
FRESHCONF="${FRESHCLAM_CONF:-/etc/clamav/freshclam.conf}"
MAILLOG="${MAIL_LOG:-/var/log/mail.log}"
BK="${BACKUP_DIR:-/root}/fleet-fix-backup-$(date +%Y%m%d-%H%M%S)"

if [[ -t 1 ]]; then
	RED=$'\e[31m' GRN=$'\e[32m' YEL=$'\e[33m' BLD=$'\e[1m' OFF=$'\e[0m'
else
	RED='' GRN='' YEL='' BLD='' OFF=''
fi
N_WARN=0
pass() { printf '  %sPASS%s  %s\n' "$GRN" "$OFF" "$*"; }
warn() { N_WARN=$((N_WARN + 1)); printf '  %sWARN%s  %s\n' "$YEL" "$OFF" "$*"; }
info() { printf '  ....  %s\n' "$*"; }
indent() { sed 's/^/        /'; }
section() { printf '\n%s== %s ==%s\n' "$BLD" "$*" "$OFF"; }
die() { printf '\n%sSTOP:%s %s\n' "$RED" "$OFF" "$*" >&2; exit 1; }
has() { command -v "$1" >/dev/null 2>&1; }

[[ $EUID -eq 0 ]] || die "run this with sudo:  sudo bash $0"

HOST="$(hostname -s 2>/dev/null || hostname)"
MEM_MB="${MEM_MB_OVERRIDE:-$(awk '/^MemTotal/ {print int($2/1024)}' /proc/meminfo)}"
SSHD_T="$(sshd -T 2>/dev/null || true)"
SSH_PORTS="$(awk '$1 == "port" {print $2}' <<<"$SSHD_T" | paste -sd, -)"
HAS_DOCKER=0; has docker && docker info >/dev/null 2>&1 && HAS_DOCKER=1
CHANGED_UNITS=0

backup() { # backup <file>...
	local f
	mkdir -p "$BK"
	for f in "$@"; do [[ -e $f ]] && cp -a "$f" "$BK/$(tr '/' '_' <<<"$f")"; done
}
write_if_changed() { # write_if_changed <path> <content>; returns 0 if written
	local path="$1" content="$2"
	if [[ -f $path ]] && [[ "$(cat "$path")" == "$content" ]]; then return 1; fi
	backup "$path"
	printf '%s\n' "$content" >"$path"
	return 0
}
unit_broken() { # true if a unit file uses shell syntax outside sh -c
	local line val
	[[ -f $1 ]] || return 1
	while IFS= read -r line; do
		val="${line#*=}"; val="${val#"${val%%[![:space:]]*}"}"
		val="${val#[-@:+!]}"; val="${val#[-@:+!]}"
		case "$val" in /bin/sh\ -c* | /bin/bash\ -c* | /usr/bin/sh\ -c* | /usr/bin/bash\ -c* | sh\ -c* | bash\ -c*) continue ;; esac
		grep -qE ' (&&|\|\||\||;|>>?|<|2>&1) ' <<<" $val " && return 0
	done < <(grep -E '^[[:space:]]*Exec[A-Za-z]*[[:space:]]*=' "$1")
	return 1
}
timers_for() { systemctl list-timers --all --no-legend 2>/dev/null | awk -v s="$1.service" '$NF == s {print $(NF-1)}'; }
retire_job() { # retire_job <name>: stop+remove a custom job and whatever timers trigger it
	local t
	for t in $(timers_for "$1"); do
		systemctl disable --now --quiet "$t" 2>/dev/null || true
		[[ -f $SD/$t ]] && { backup "$SD/$t"; rm -f "$SD/$t"; }
	done
	[[ -f $SD/$1.timer ]] && { backup "$SD/$1.timer"; rm -f "$SD/$1.timer"; }
	[[ -f $SD/$1.service ]] && { backup "$SD/$1.service"; rm -f "$SD/$1.service"; }
	CHANGED_UNITS=1
}
port_listening() { local out; out="$(ss -Htln "sport = :$1" 2>/dev/null)"; [[ -n $out ]]; }

# --- standard job definitions ------------------------------------------------
timer_unit() { # timer_unit <description> <OnCalendar> <persistent> [randomized delay]
	printf '[Unit]\nDescription=%s\n\n[Timer]\nOnCalendar=%s\n' "$1" "$2"
	[[ -n ${4:-} ]] && printf 'RandomizedDelaySec=%s\n' "$4"
	printf '# Never at boot%s.\nPersistent=%s\n\n[Install]\nWantedBy=timers.target' \
		"$([[ $3 == false ]] && echo ', and no catch-up runs at boot either')" "$3"
}
std_rkhunter_service() {
	cat <<EOF
[Unit]
Description=rkhunter rootkit scan
Documentation=man:rkhunter(8)

[Service]
Type=oneshot
Nice=19
IOSchedulingClass=idle
# Exit 0 = clean. Exit 1 = warnings found: this job then shows as failed and the
# login notice lists it. Full details: /var/log/rkhunter.log
ExecStart=$1 --check --skip-keypress --report-warnings-only --nocolors
EOF
}
std_chkrootkit_service() {
	cat <<EOF
[Unit]
Description=chkrootkit scan (findings in: journalctl -t chkrootkit)

[Service]
Type=oneshot
Nice=19
IOSchedulingClass=idle
# Findings are logged, not alarmed: chkrootkit gives regular false positives on
# modern Ubuntu, and a notice that cries wolf at every login gets ignored.
ExecStart=/bin/sh -c '$1 -q 2>&1 | logger -t chkrootkit'
EOF
}
std_freshclam_service() {
	cat <<EOF
[Unit]
Description=Update ClamAV database
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
Nice=19
IOSchedulingClass=idle
OOMScoreAdjust=1000
ExecStart=$1 --quiet
EOF
}
std_clamscan_service() { # $1 clamscan path, rest: folders
	local bin="$1"; shift
	cat <<EOF
[Unit]
Description=ClamAV weekly scan of writable folders
After=freshclam.service

[Service]
Type=oneshot
Nice=19
IOSchedulingClass=idle
# If memory runs out, the kernel kills this scan first - never the sites or apps
OOMScoreAdjust=1000
# clamscan exits 0 = clean, 1 = something found, 2 = error. Anything but 0 shows red.
ExecStart=$bin --recursive --infected $*
EOF
}
MOTD_SCRIPT_CONTENT="$(cat <<'EOF'
#!/bin/sh
# Lists failed services at every SSH login, so a dead service cannot hide.
failed="$(systemctl list-units --state=failed --no-legend --plain 2>/dev/null | awk '{print $1}' | tr '\n' ' ')"
[ -n "$failed" ] || exit 0
printf '\n  *** FAILED SERVICES: %s\n  *** Details: systemctl --failed\n\n' "$failed"
EOF
)"

# =============================================================================
# 1. Work out the plan
# =============================================================================
PLAN=()
plan() { PLAN+=("$*"); }

# login notice
NEED_MOTD=0
[[ -d $MOTD_DIR && ! -x $MOTD_DIR/98-failed-services ]] && { NEED_MOTD=1; plan "login notice: list failed services at every SSH login"; }

# rkhunter / chkrootkit
RKH="$(command -v rkhunter || true)"; CKR="$(command -v chkrootkit || true)"
NEED_RKH=0; NEED_RKCONF=0; NEED_CKR=0
if [[ -n $RKH ]]; then
	if [[ "$(cat "$SD/rkhunter.service" 2>/dev/null)" != "$(std_rkhunter_service "$RKH")" ||
		"$(cat "$SD/rkhunter.timer" 2>/dev/null)" != "$(timer_unit 'Nightly rkhunter scan, 02:40' '*-*-* 02:40:00' false)" ]]; then
		NEED_RKH=1; plan "rkhunter: standard nightly job at 02:40, never at boot"
	fi
	if { [[ -f $RKCONF ]] && grep -qE '^[[:space:]]*WEB_CMD="?/bin/false"?[[:space:]]*$' "$RKCONF"; } ||
		{ [[ -f $DEFD/rkhunter ]] && ! grep -qx 'APT_AUTOGEN="true"' "$DEFD/rkhunter"; }; then
		NEED_RKCONF=1; plan "rkhunter: fix Ubuntu's WEB_CMD bug, refresh fingerprints after package installs"
	fi
elif [[ -f $SD/rkhunter.service ]]; then
	NEED_RKH=2; plan "rkhunter: not installed - retire its leftover job"
fi
if [[ -n $CKR ]]; then
	if [[ "$(cat "$SD/chkrootkit.service" 2>/dev/null)" != "$(std_chkrootkit_service "$CKR")" ||
		"$(cat "$SD/chkrootkit.timer" 2>/dev/null)" != "$(timer_unit 'Nightly chkrootkit scan, 02:20' '*-*-* 02:20:00' false)" ]]; then
		NEED_CKR=1; plan "chkrootkit: standard nightly job at 02:20, findings logged"
	fi
elif [[ -f $SD/chkrootkit.service ]]; then
	NEED_CKR=2; plan "chkrootkit: not installed - retire its leftover job"
fi

# apt
NEED_APT_RETIRE=0; NEED_UU=0
unit_broken "$SD/apt.service" && { NEED_APT_RETIRE=1; plan "apt: retire the broken custom apt job (it has never worked)"; }
if [[ "$(dpkg-query -W -f='${Status}' unattended-upgrades 2>/dev/null)" == *"install ok installed"* ]]; then
	apt_dump="$(apt-config dump 2>/dev/null || true)"
	if ! grep -qE 'APT::Periodic::Unattended-Upgrade "1"' <<<"$apt_dump" ||
		! systemctl is-enabled --quiet apt-daily-upgrade.timer 2>/dev/null ||
		! systemctl is-enabled --quiet apt-daily.timer 2>/dev/null; then
		NEED_UU=1; plan "apt: switch on automatic security updates (unattended-upgrades; never reboots)"
	fi
fi

# ClamAV
CLAMSCAN="$(command -v clamscan || true)"; FRESHCLAM="$(command -v freshclam || true)"
NEED_FRESH=0; NEED_CLAMTIMER=0; NEED_CLAMSVC=0; NEED_TESTDB=0
SCAN_DIRS=()
for d in /home /root /srv /opt /var/www /tmp /var/tmp /dev/shm; do [[ -d $d ]] && SCAN_DIRS+=("$d"); done
if [[ -n $FRESHCLAM && ( -f $SD/freshclam.service || -f $SD/freshclam.timer ) ]]; then
	if [[ "$(cat "$SD/freshclam.service" 2>/dev/null)" != "$(std_freshclam_service "$FRESHCLAM")" ||
		"$(cat "$SD/freshclam.timer" 2>/dev/null)" != "$(timer_unit 'Daily ClamAV database update, around 03:30' '*-*-* 03:30:00' true 30min)" ]]; then
		NEED_FRESH=1; plan "ClamAV: database update nightly around 03:30, never at boot"
	fi
	if ((MEM_MB < 2048)) && [[ -f $FRESHCONF ]] && ! grep -qx 'TestDatabases no' "$FRESHCONF"; then
		NEED_TESTDB=1; plan "ClamAV: skip the ~650 MB database test-load after updates (this box has ${MEM_MB} MB)"
	fi
fi
if [[ -n $CLAMSCAN && -f $SD/clamscan.service ]]; then
	if [[ "$(cat "$SD/clamscan.timer" 2>/dev/null)" != "$(timer_unit 'Weekly ClamAV scan, Sundays 04:15' 'Sun *-*-* 04:15:00' false)" ]]; then
		NEED_CLAMTIMER=1; plan "ClamAV: weekly scan Sundays 04:15, never at boot"
	fi
	if { ((MEM_MB < 2048)) && [[ "$(cat "$SD/clamscan.service")" != "$(std_clamscan_service "$CLAMSCAN" "${SCAN_DIRS[@]}")" ]]; } ||
		unit_broken "$SD/clamscan.service"; then
		NEED_CLAMSVC=1; plan "ClamAV: scan only writable folders at lowest priority; scan dies first if memory runs out"
	fi
fi

# fail2ban
NEED_F2B=0; F2B_WHY=""
if has fail2ban-client; then
	f2b_test="$(fail2ban-client -t 2>&1)" && f2b_ok=1 || f2b_ok=0
	f2b_dump="$(fail2ban-client -d 2>/dev/null | grep -F "'sshd'" || true)"
	dump_ports="$(grep -oE -- '--dports [^ ]+' <<<"$f2b_dump" | head -n 1 | awk '{print $2}')"
	[[ -z $dump_ports ]] && dump_ports="$(grep -oE "\['port', '[^']*'\]" <<<"$f2b_dump" | head -n 1 | cut -d"'" -f4)"
	if ((!f2b_ok)); then F2B_WHY="its config is broken ($(grep -m1 -o 'ERROR.*' <<<"$f2b_test" | cut -c1-90))"
	elif ! systemctl is-active --quiet fail2ban; then F2B_WHY="it isn't running"
	elif [[ -n $SSH_PORTS && -n $dump_ports && $dump_ports != "$SSH_PORTS" ]]; then F2B_WHY="its SSH jail guards '$dump_ports' instead of port $SSH_PORTS"
	fi
	[[ -n $F2B_WHY ]] && { NEED_F2B=1; plan "fail2ban: rewrite jail.local - $F2B_WHY"; }
fi

# certbot
NEED_CERT_DEL=(); NEED_CERTBOT_TIMER=0; CERT_KEPT=()
if has certbot; then
	certs="$(certbot certificates 2>/dev/null || true)"
	refs="$( { has nginx && nginx -T 2>/dev/null; } ; grep -rhs '/etc/letsencrypt' /etc/postfix /etc/dovecot /etc/apache2 /etc/caddy 2>/dev/null)"
	while IFS='|' read -r name state; do
		[[ -z $name ]] && continue
		[[ $state == INVALID ]] || continue
		if grep -qF "/etc/letsencrypt/live/$name/" <<<"$refs"; then
			CERT_KEPT+=("$name")
		else
			NEED_CERT_DEL+=("$name")
		fi
	done < <(awk '/Certificate Name:/ {n=$3} /Expiry Date:/ {s = ($0 ~ /INVALID/) ? "INVALID" : "VALID"; print n "|" s}' <<<"$certs")
	((${#NEED_CERT_DEL[@]})) && plan "certbot: delete expired certificates nothing uses: ${NEED_CERT_DEL[*]}"
	web_caddy=0
	systemctl is-active --quiet caddy 2>/dev/null && web_caddy=1
	if ((HAS_DOCKER)); then
		dps="$(docker ps --format '{{.Image}} {{.Ports}}' 2>/dev/null || true)"
		grep -qE 'caddy.*:443->' <<<"$dps" && web_caddy=1
	fi
	mail_uses_certbot=0
	grep -rqsE '/etc/letsencrypt' /etc/postfix /etc/dovecot 2>/dev/null && mail_uses_certbot=1
	if ((web_caddy && !mail_uses_certbot)) && ! systemctl is-active --quiet nginx 2>/dev/null &&
		systemctl is-enabled --quiet certbot.timer 2>/dev/null; then
		NEED_CERTBOT_TIMER=1; plan "certbot: switch off its renewal timer - Caddy handles certificates on this box"
	fi
fi

# ports
NEED_PORTS=()
ufw_rules=""
has ufw && ufw_rules="$(ufw status 2>/dev/null)"
if grep -q 'Status: active' <<<"$ufw_rules"; then
	for p in 8080 8081 8083; do
		grep -qE "^$p/tcp[[:space:]]+ALLOW( IN)?[[:space:]]+Anywhere[[:space:]]*$" <<<"$ufw_rules" && NEED_PORTS+=("$p")
	done
	if ((${#NEED_PORTS[@]})); then
		if ((HAS_DOCKER)); then
			plan "ports: close ${NEED_PORTS[*]} to the internet (kept open to Docker's own networks)"
		else
			plan "ports: close ${NEED_PORTS[*]} to the internet (the sites reach them from inside the box)"
		fi
	fi
fi

# SSH
NEED_SSH=0
grep -qx 'passwordauthentication yes' <<<"$SSHD_T" && { NEED_SSH=1; plan "SSH: keys only (no more password logins)"; }

# =============================================================================
# 2. Show the plan and ask
# =============================================================================
section "Plan for $HOST (${MEM_MB} MB RAM, SSH on port ${SSH_PORTS:-?})"
if ((${#PLAN[@]} == 0)); then
	pass "nothing to fix"
else
	printf '  %s\n' "${PLAN[@]/#/- }"
	printf '\n'
	read -r -p "  Type yes to apply all of the above: " answer
	[[ $answer == yes ]] || die "nothing changed"
fi

# =============================================================================
# 3. Apply
# =============================================================================
if ((NEED_MOTD)); then
	section "Login notice"
	printf '%s\n' "$MOTD_SCRIPT_CONTENT" >"$MOTD_DIR/98-failed-services"
	chmod 0755 "$MOTD_DIR/98-failed-services"
	pass "installed"
fi

if ((NEED_RKH || NEED_RKCONF || NEED_CKR)); then
	section "Rootkit scanners"
	if ((NEED_RKH == 1)); then
		for t in $(timers_for rkhunter); do
			[[ $t == rkhunter.timer ]] && continue
			systemctl disable --now --quiet "$t" 2>/dev/null; backup "$SD/$t"; rm -f "$SD/$t"
		done
		write_if_changed "$SD/rkhunter.service" "$(std_rkhunter_service "$RKH")"
		write_if_changed "$SD/rkhunter.timer" "$(timer_unit 'Nightly rkhunter scan, 02:40' '*-*-* 02:40:00' false)"
		CHANGED_UNITS=1
		pass "rkhunter job rebuilt"
	elif ((NEED_RKH == 2)); then
		retire_job rkhunter; pass "rkhunter's leftover job retired"
	fi
	if ((NEED_RKCONF)); then
		if [[ -f $RKCONF ]] && grep -qE '^[[:space:]]*WEB_CMD="?/bin/false"?[[:space:]]*$' "$RKCONF"; then
			backup "$RKCONF"; sed -i -E 's|^[[:space:]]*WEB_CMD=.*|WEB_CMD=""|' "$RKCONF"
		fi
		if [[ -f $DEFD/rkhunter ]]; then
			backup "$DEFD/rkhunter"
			for kv in 'APT_AUTOGEN="true"' 'CRON_DAILY_RUN="false"'; do
				key="${kv%%=*}"
				if grep -qE "^[[:space:]]*$key=" "$DEFD/rkhunter"; then
					sed -i -E "s|^[[:space:]]*$key=.*|$kv|" "$DEFD/rkhunter"
				else
					printf '%s\n' "$kv" >>"$DEFD/rkhunter"
				fi
			done
		fi
		pass "rkhunter settings fixed"
	fi
	if [[ -n $RKH ]] && ((NEED_RKH || NEED_RKCONF)); then
		if "$RKH" --propupd --nocolors >/dev/null 2>&1; then pass "rkhunter fingerprints recorded"; else warn "couldn't record rkhunter fingerprints"; fi
	fi
	if ((NEED_CKR == 1)); then
		for t in $(timers_for chkrootkit); do
			[[ $t == chkrootkit.timer ]] && continue
			systemctl disable --now --quiet "$t" 2>/dev/null; backup "$SD/$t"; rm -f "$SD/$t"
		done
		write_if_changed "$SD/chkrootkit.service" "$(std_chkrootkit_service "$CKR")"
		write_if_changed "$SD/chkrootkit.timer" "$(timer_unit 'Nightly chkrootkit scan, 02:20' '*-*-* 02:20:00' false)"
		CHANGED_UNITS=1
		pass "chkrootkit job rebuilt"
	elif ((NEED_CKR == 2)); then
		retire_job chkrootkit; pass "chkrootkit's leftover job retired"
	fi
fi

if ((NEED_APT_RETIRE || NEED_UU)); then
	section "Updates"
	if ((NEED_APT_RETIRE)); then retire_job apt; pass "broken custom apt job retired"; fi
	if ((NEED_UU)); then
		backup "$APTD/20auto-upgrades"
		printf 'APT::Periodic::Update-Package-Lists "1";\nAPT::Periodic::Unattended-Upgrade "1";\n' >"$APTD/20auto-upgrades"
		systemctl enable --now --quiet apt-daily.timer apt-daily-upgrade.timer 2>/dev/null || true
		pass "automatic security updates are on"
	fi
	if [[ -f $SD/apt.service ]]; then
		info "a custom apt job remains (written correctly). What it runs:"
		grep -E '^[[:space:]]*ExecStart' "$SD/apt.service" | indent
	fi
fi

if ((NEED_FRESH || NEED_TESTDB || NEED_CLAMTIMER || NEED_CLAMSVC)); then
	section "ClamAV"
	if ((NEED_FRESH)); then
		write_if_changed "$SD/freshclam.service" "$(std_freshclam_service "$FRESHCLAM")"
		write_if_changed "$SD/freshclam.timer" "$(timer_unit 'Daily ClamAV database update, around 03:30' '*-*-* 03:30:00' true 30min)"
		CHANGED_UNITS=1; pass "database update: nightly around 03:30, never at boot"
	fi
	if ((NEED_TESTDB)); then
		backup "$FRESHCONF"
		if grep -q '^TestDatabases' "$FRESHCONF"; then sed -i 's/^TestDatabases.*/TestDatabases no/' "$FRESHCONF"; else echo 'TestDatabases no' >>"$FRESHCONF"; fi
		pass "database test-load switched off"
	fi
	if ((NEED_CLAMTIMER)); then
		write_if_changed "$SD/clamscan.timer" "$(timer_unit 'Weekly ClamAV scan, Sundays 04:15' 'Sun *-*-* 04:15:00' false)"
		CHANGED_UNITS=1; pass "weekly scan: Sundays 04:15, never at boot"
	fi
	if ((NEED_CLAMSVC)); then
		write_if_changed "$SD/clamscan.service" "$(std_clamscan_service "$CLAMSCAN" "${SCAN_DIRS[@]}")"
		CHANGED_UNITS=1; pass "scan covers: ${SCAN_DIRS[*]}"
	fi
	pgrep -x clamscan >/dev/null && { pkill -x clamscan; info "stopped a scan that was running"; }
fi

if ((CHANGED_UNITS)); then
	systemctl daemon-reload
	for u in rkhunter chkrootkit apt clamscan freshclam; do systemctl reset-failed "$u.service" 2>/dev/null || true; done
	for t in rkhunter chkrootkit freshclam clamscan; do [[ -f $SD/$t.timer ]] && systemctl enable --now --quiet "$t.timer" 2>/dev/null; done
fi

if ((NEED_F2B)); then
	section "fail2ban"
	old_custom=""
	if [[ -f $F2B/jail.local && -f $F2B/jail.conf ]]; then
		old_custom="$(diff <(grep -vE '^[[:space:]]*([#;]|$)' "$F2B/jail.conf") <(grep -vE '^[[:space:]]*([#;]|$)' "$F2B/jail.local") | grep '^>' | sed 's/^> //' || true)"
	fi
	backup "$F2B/jail.local"
	logtarget="$(cat "$F2B"/fail2ban.conf "$F2B"/fail2ban.local "$F2B"/fail2ban.d/*.conf "$F2B"/fail2ban.d/*.local 2>/dev/null |
		grep -E '^[[:space:]]*logtarget[[:space:]]*=' | tail -n 1 | sed -E 's/^[^=]*=[[:space:]]*//; s/[[:space:]]+$//' || true)"
	jails="sshd"
	{
		printf '# /etc/fail2ban/jail.local - this server'"'"'s own settings, nothing else.\n'
		printf '# Written by fleet-fix.sh on %s. Old file: %s\n' "$(date '+%Y-%m-%d %H:%M')" "$BK"
		printf '# Everything else comes from jail.conf (the package'"'"'s file - never edit it).\n'
		printf '# After editing:  sudo fail2ban-client -t && sudo systemctl restart fail2ban\n\n'
		printf '[DEFAULT]\n# Never ban this machine itself.\nignoreip = 127.0.0.1/8 ::1\nbantime  = 1h\nfindtime = 10m\nmaxretry = 5\n\n'
		printf '# SSH listens on %s here, not 22 - so that is where bans must land.\n[sshd]\nenabled  = true\nport     = %s\nmaxretry = 3\n' "$SSH_PORTS" "$SSH_PORTS"
		if [[ $logtarget == /var/log/fail2ban.log ]]; then
			printf '\n# Repeat offenders (banned 5 times in a day) are shut out of every port for a week.\n[recidive]\nenabled = true\n'
			jails+=", recidive"
		fi
		if systemctl is-active --quiet dovecot 2>/dev/null && [[ -f $MAILLOG ]]; then
			printf '\n# Mail logins (IMAP).\n[dovecot]\nenabled = true\n'
			jails+=", dovecot"
		fi
		if systemctl is-active --quiet postfix 2>/dev/null && [[ -f $MAILLOG ]] && { port_listening 587 || port_listening 465; }; then
			printf '\n# Mail-sending logins (SMTP AUTH on 587/465).\n[postfix-sasl]\nenabled = true\n'
			jails+=", postfix-sasl"
		fi
	} >"$F2B/jail.local"
	if ! t_out="$(fail2ban-client -t 2>&1)"; then
		warn "fail2ban rejected the new config - putting the old one back:"
		printf '%s\n' "$t_out" | tail -n 8 | indent
		if [[ -f $BK/$(tr '/' '_' <<<"$F2B/jail.local") ]]; then
			cp -a "$BK/$(tr '/' '_' <<<"$F2B/jail.local")" "$F2B/jail.local"
		else
			rm -f "$F2B/jail.local"
		fi
	else
		systemctl reset-failed fail2ban 2>/dev/null || true
		systemctl enable --quiet fail2ban
		if systemctl restart fail2ban && sleep 3 && systemctl is-active --quiet fail2ban; then
			pass "fail2ban running with: $jails (SSH jail on port $SSH_PORTS)"
		else
			warn "fail2ban didn't stay up:"; journalctl -u fail2ban -n 10 --no-pager | indent
		fi
		if [[ -n $old_custom ]]; then
			info "custom lines from the old jail.local (saved in the backup):"
			printf '%s\n' "$old_custom" | head -n 20 | indent
		fi
	fi
fi

if ((${#NEED_CERT_DEL[@]} || NEED_CERTBOT_TIMER)); then
	section "certbot"
	for name in "${NEED_CERT_DEL[@]}"; do
		if certbot delete --cert-name "$name" --non-interactive >/dev/null 2>&1; then pass "deleted expired $name"; else warn "couldn't delete $name - run: sudo certbot delete --cert-name $name"; fi
	done
	if ((NEED_CERTBOT_TIMER)); then
		systemctl disable --now --quiet certbot.timer 2>/dev/null || true
		pass "certbot's renewal timer is off (Caddy renews its own certificates)"
	fi
	systemctl reset-failed certbot.service 2>/dev/null || true
fi
if ((${#CERT_KEPT[@]})); then
	section "Expired certificates still named in a web server config (not touched)"
	for name in "${CERT_KEPT[@]}"; do
		if systemctl is-active --quiet nginx 2>/dev/null; then
			warn "$name: expired, but nginx (running) still points at it. If the domain is gone, remove that site first - ask Claude."
		else
			info "$name: expired; only named in the old, switched-off nginx config - harmless, goes when nginx is uninstalled."
		fi
	done
fi

if ((${#NEED_PORTS[@]})); then
	section "Ports"
	for p in "${NEED_PORTS[@]}"; do
		if ((HAS_DOCKER)); then
			ufw allow from 172.16.0.0/12 to any port "$p" proto tcp comment "Docker networks only" >/dev/null &&
				info "port $p: open to Docker's own networks (172.16.0.0/12)"
		fi
		ufw delete allow "$p/tcp" >/dev/null && pass "port $p closed to the internet (to undo: sudo ufw allow $p/tcp)"
	done
fi

if ((NEED_SSH)); then
	section "SSH: keys only"
	mkdir -p "$BK/ssh"
	cp -a "$SSHD_DIR/sshd_config" "$BK/ssh/"
	[[ -d $SSHD_DIR/sshd_config.d ]] && cp -a "$SSHD_DIR/sshd_config.d" "$BK/ssh/"
	mkdir -p "$SSHD_DIR/sshd_config.d"
	printf '# Keys only. Named 00- so it is read first - in sshd the first value wins.\nPasswordAuthentication no\nKbdInteractiveAuthentication no\n' \
		>"$SSHD_DIR/sshd_config.d/00-keys-only.conf"
	sed -i -E 's/^[[:space:]]*PasswordAuthentication[[:space:]]+yes/PasswordAuthentication no/' \
		"$SSHD_DIR/sshd_config" "$SSHD_DIR"/sshd_config.d/*.conf 2>/dev/null
	if sshd -t 2>/dev/null && grep -qx 'passwordauthentication no' <<<"$(sshd -T 2>/dev/null)"; then
		systemctl reload ssh 2>/dev/null || systemctl reload sshd 2>/dev/null || true
		pass "SSH is keys only. Your current session stays connected."
		info "Before closing this window: open a NEW PowerShell window and run  ssh <this server's alias>"
	else
		warn "sshd didn't accept the change - putting the old SSH files back"
		cp -a "$BK/ssh/sshd_config" "$SSHD_DIR/sshd_config"
		rm -rf "$SSHD_DIR/sshd_config.d"
		if [[ -d $BK/ssh/sshd_config.d ]]; then cp -a "$BK/ssh/sshd_config.d" "$SSHD_DIR/"; else mkdir -p "$SSHD_DIR/sshd_config.d"; fi
	fi
fi

# =============================================================================
# 4. Reports (no changes)
# =============================================================================
section "For your information (nothing changed here)"
if ((HAS_DOCKER)); then
	open_ports="$(docker ps --format '{{.Names}} {{.Ports}}' 2>/dev/null | grep -E '0\.0\.0\.0:|:::' || true)"
	if [[ -n $open_ports ]]; then
		info "Docker containers reachable from the whole internet (Docker skips ufw):"
		printf '%s\n' "$open_ports" | indent
		while read -r cname _; do
			dir="$(docker inspect "$cname" --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' 2>/dev/null)"
			[[ -n $dir ]] && info "  $cname comes from the compose project in: $dir"
		done <<<"$open_ports"
		if [[ -f /etc/caddy/Caddyfile ]]; then
			info "where Caddy forwards visitors (compare with the ports above):"
			grep -nE 'reverse_proxy' /etc/caddy/Caddyfile | indent
		fi
	fi
fi
if [[ -s /root/.ssh/authorized_keys ]]; then
	info "root's SSH keys (root can't log in over SSH, but worth knowing what's there):"
	cut -c1-110 /root/.ssh/authorized_keys | indent
fi
disk_pct="$(df --output=pcent / | tail -n 1 | tr -dc '0-9')"
if ((disk_pct >= 80)); then
	warn "disk is ${disk_pct}% full. Biggest places:"
	du -xsh /var/lib/docker /var/log /var/cache /home /var/www /opt /srv 2>/dev/null | sort -h | tail -n 7 | indent
	((HAS_DOCKER)) && docker system df 2>/dev/null | indent
fi

# =============================================================================
# 5. Check
# =============================================================================
section "Checking"
if [[ -f $SD/rkhunter.service ]] && ((NEED_RKH || NEED_RKCONF)); then
	info "running rkhunter once (a minute or two)..."
	if systemctl start rkhunter.service; then
		pass "rkhunter: clean"
	else
		warn "rkhunter found things worth a look:"
		{ grep -E 'Warning:' "$RKLOG" 2>/dev/null | tail -n 25 || true; } | indent
	fi
fi
if [[ -x $MOTD_DIR/98-failed-services ]]; then
	now="$("$MOTD_DIR/98-failed-services")"
	if [[ -n $now ]]; then info "the login notice now says:"; printf '%s\n' "$now" | indent; else pass "login notice: no failed services"; fi
fi

if ((N_WARN)); then
	printf '\n  %sDone on %s, with %d thing(s) to look at above.%s Backups: %s\n' "$YEL" "$HOST" "$N_WARN" "$OFF" "$BK"
else
	printf '\n  %sDone on %s.%s Backups: %s\n' "$GRN" "$HOST" "$OFF" "$BK"
fi
