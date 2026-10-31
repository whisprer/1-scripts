#!/usr/bin/env bash
# server-audit.sh - read-only security & health audit. Changes NOTHING.
# Checks everything that turned out broken on LON1 (Oct 2026):
#   web server boot race, custom systemd jobs using shell syntax (never worked),
#   jobs that run at boot, ClamAV memory, rkhunter/chkrootkit, automatic updates,
#   fail2ban (running? guarding the real SSH port?), SSH password logins, accounts
#   and SSH keys, firewall and open ports, Docker ports, certificates.
#
#   sudo bash server-audit.sh
#
# Writes the full report to ~/server-audit-<hostname>.txt (readable only by you)
# and prints the FLAG summary at the end.
set -uo pipefail
export LC_ALL=C

[[ $EUID -eq 0 ]] || { echo "run with sudo:  sudo bash $0"; exit 1; }

HOST="$(hostname -s 2>/dev/null || hostname)"
OWNER="${SUDO_USER:-root}"
OWNER_HOME="$(getent passwd "$OWNER" 2>/dev/null | cut -d: -f6 || true)"
REPORT="${REPORT_PATH:-${OWNER_HOME:-/root}/server-audit-$HOST.txt}"
FLAGS=()
flag() { FLAGS+=("$*"); printf 'FLAG: %s\n' "$*"; }
has() { command -v "$1" >/dev/null 2>&1; }
section() { printf '\n==================== %s ====================\n' "$*"; }
systemd_up() { [[ -d /run/systemd/system ]]; }

audit() {
	section "Machine"
	echo "host: $HOST   date: $(date '+%Y-%m-%d %H:%M %Z')"
	grep -E '^PRETTY_NAME=' /etc/os-release 2>/dev/null | cut -d= -f2- | tr -d '"'
	echo "kernel: $(uname -r)   up: $(uptime -p 2>/dev/null || uptime)"
	mem_mb="$(awk '/^MemTotal/ {print int($2/1024)}' /proc/meminfo)"
	free -h 2>/dev/null
	df -h / 2>/dev/null | tail -n 1 | awk '{print "disk /: " $3 " used of " $2 " (" $5 ")"}'
	echo; echo "biggest memory users:"; ps aux --sort=-%mem 2>/dev/null | head -n 6

	section "Failed services"
	if systemd_up; then
		failed="$(systemctl list-units --state=failed --no-legend --plain 2>/dev/null | awk '{print $1}')"
		if [[ -n $failed ]]; then
			echo "$failed"
			flag "failed services: $(echo "$failed" | tr '\n' ' ')"
		else
			echo "none"
		fi
		if [[ -x /etc/update-motd.d/98-failed-services ]]; then
			echo "login notice for failed services: installed"
		else
			flag "no login notice for failed services (a dead service can go unnoticed)"
		fi
	else
		echo "systemd isn't running here - skipped"
	fi

	section "Custom systemd jobs using shell syntax (systemd runs these WITHOUT a shell)"
	bad_units=0
	for f in /etc/systemd/system/*.service /etc/systemd/system/*.service.d/*.conf; do
		[[ -f $f && ! -L $f ]] || continue
		while IFS= read -r line; do
			val="${line#*=}"
			val="${val#"${val%%[![:space:]]*}"}"
			val="${val#[-@:+!]}"; val="${val#[-@:+!]}"
			case "$val" in
				/bin/sh\ -c* | /bin/bash\ -c* | /usr/bin/sh\ -c* | /usr/bin/bash\ -c* | sh\ -c* | bash\ -c* | /usr/bin/env\ bash\ -c* | /usr/bin/env\ sh\ -c*) continue ;;
			esac
			if grep -qE ' (&&|\|\||\||;|>>?|<|2>&1) ' <<<" $val "; then
				echo "$f:  $line"
				bad_units=$((bad_units + 1))
			fi
		done < <(grep -E '^[[:space:]]*Exec(Start|StartPre|StartPost|Stop|StopPost|Reload|Condition)[[:space:]]*=' "$f")
	done
	if ((bad_units)); then
		flag "$bad_units custom job line(s) use shell syntax - those jobs have never worked as written"
	else
		echo "none found"
	fi

	section "Timers (what runs when) - and anything that runs at boot"
	systemd_up && systemctl list-timers --all --no-pager 2>/dev/null | head -n 40
	boot_timers="$(grep -lE '^[[:space:]]*OnBootSec' /etc/systemd/system/*.timer 2>/dev/null | xargs -r -n1 basename)"
	if [[ -n $boot_timers ]]; then
		echo; echo "custom timers that fire after boot: $boot_timers"
		flag "custom jobs run at every boot: $(echo "$boot_timers" | tr '\n' ' ')"
	fi

	section "Virus scanning (ClamAV)"
	if has clamscan || has freshclam; then
		echo "clamscan: $(command -v clamscan || echo none)   freshclam: $(command -v freshclam || echo none)"
		pgrep -a clamd 2>/dev/null && echo "(clamd daemon is running - it holds the virus database in memory permanently)"
		if systemd_up; then
			systemctl list-timers --all --no-pager 2>/dev/null | grep -iE 'clam' || true
		fi
		for f in /etc/systemd/system/*clam*.service /etc/systemd/system/*clam*.timer; do
			[[ -f $f ]] && { echo "--- $f"; grep -vE '^[[:space:]]*(#|$)' "$f"; }
		done
		grep -h '^TestDatabases' /etc/clamav/freshclam.conf 2>/dev/null
		clam_timers=(/etc/systemd/system/*clam*.timer)
		if ((mem_mb < 2048)) && [[ -f ${clam_timers[0]} ]]; then
			flag "ClamAV scans scheduled on a ${mem_mb} MB box - each scan needs ~650 MB"
		fi
	else
		echo "ClamAV not installed"
	fi

	section "Rootkit scanners"
	for tool in rkhunter chkrootkit; do
		echo "--- $tool: $(command -v "$tool" || echo 'not installed')"
		for f in /etc/systemd/system/"$tool".service /etc/systemd/system/"$tool".timer; do
			[[ -f $f ]] && grep -E '^[[:space:]]*(ExecStart|OnCalendar|OnBootSec)' "$f" | sed "s#^#  $(basename "$f"): #"
		done
	done
	if [[ -f /etc/rkhunter.conf ]] && grep -qE '^[[:space:]]*WEB_CMD="?/bin/false"?' /etc/rkhunter.conf; then
		flag "rkhunter.conf has Ubuntu's broken WEB_CMD=\"/bin/false\""
	fi
	[[ -f /etc/default/rkhunter ]] && grep -E '^(CRON_DAILY_RUN|APT_AUTOGEN)=' /etc/default/rkhunter

	section "Updates"
	if has apt-get; then
		apt-config dump 2>/dev/null | grep -E 'APT::Periodic::(Update-Package-Lists|Unattended-Upgrade) ' || echo "(no APT::Periodic settings)"
		uu_status="$(dpkg-query -W -f='${Status}' unattended-upgrades 2>/dev/null || true)"
		apt_dump="$(apt-config dump 2>/dev/null || true)"
		if [[ $uu_status == *"install ok installed"* ]]; then
			echo "unattended-upgrades: installed"
			grep -qE 'APT::Periodic::Unattended-Upgrade "1"' <<<"$apt_dump" ||
				flag "unattended-upgrades installed but switched off in APT::Periodic"
			if systemd_up && ! systemctl is-enabled --quiet apt-daily-upgrade.timer 2>/dev/null; then
				flag "apt-daily-upgrade.timer is disabled - automatic security updates never run"
			fi
		else
			flag "unattended-upgrades not installed - no automatic security updates"
		fi
		systemd_up && systemctl is-enabled apt-daily.timer apt-daily-upgrade.timer 2>/dev/null | paste -sd' ' - | sed 's/^/apt-daily, apt-daily-upgrade timers: /'
		upg="$(apt-get -s upgrade 2>/dev/null | grep -c '^Inst' || true)"
		sec="$(apt-get -s upgrade 2>/dev/null | grep '^Inst' | grep -ci 'security' || true)"
		echo "pending upgrades: $upg (security: $sec)"
		((sec > 0)) && flag "$sec security update(s) waiting"
		[[ -f /var/run/reboot-required ]] && flag "a reboot is pending (kernel or library update)"
	fi

	section "fail2ban"
	ssh_ports="$(sshd -T 2>/dev/null | awk '$1 == "port" {print $2}' | paste -sd, -)"
	if has fail2ban-client; then
		if systemd_up; then
			echo "enabled: $(systemctl is-enabled fail2ban 2>/dev/null)   active: $(systemctl is-active fail2ban 2>/dev/null)"
			systemctl show fail2ban -p Restart -p RestartPreventExitStatus 2>/dev/null
			systemctl is-active --quiet fail2ban || flag "fail2ban is NOT running"
		fi
		t="$(fail2ban-client -t 2>&1 | tail -n 3)"
		echo "config test: $t"
		grep -q 'ERROR' <<<"$t" && flag "fail2ban config is broken: $(grep -m1 ERROR <<<"$t")"
		fail2ban-client status 2>/dev/null | grep -i 'jail list'
		dump_ports="$(fail2ban-client -d 2>/dev/null | grep -F "'sshd'" | grep -iE "port" | head -n 3)"
		echo "sshd jail lines mentioning a port (config dump):"
		printf '%s\n' "$dump_ports" | cut -c1-220 | sed 's/^/  /'
		if [[ -n $ssh_ports && -n $dump_ports ]] && ! grep -qF "${ssh_ports%%,*}" <<<"$dump_ports"; then
			flag "fail2ban's sshd jail doesn't seem to guard the real SSH port ($ssh_ports)"
		fi
		[[ -f /etc/fail2ban/jail.local ]] && echo "jail.local: $(wc -l </etc/fail2ban/jail.local) lines"
	else
		flag "fail2ban not installed"
	fi

	section "SSH"
	sshd -T 2>/dev/null | grep -E '^(port|listenaddress|passwordauthentication|kbdinteractiveauthentication|permitrootlogin|pubkeyauthentication|maxauthtries|allowusers|allowgroups) '
	grep -nE '^[[:space:]]*(Include|PasswordAuthentication|PermitRootLogin|KbdInteractiveAuthentication|ChallengeResponseAuthentication|Match)' \
		/etc/ssh/sshd_config /etc/ssh/sshd_config.d/*.conf 2>/dev/null
	sshd_t="$(sshd -T 2>/dev/null || true)"
	grep -qx 'passwordauthentication yes' <<<"$sshd_t" && flag "SSH accepts passwords"
	grep -qx 'permitrootlogin yes' <<<"$sshd_t" && flag "SSH allows root to log in with a password"
	echo; echo "successful logins by method / user / address (auth logs on disk):"
	zgrep -hE 'Accepted (password|publickey|keyboard-interactive)' /var/log/auth.log* /var/log/secure* 2>/dev/null |
		awk '{for(i=1;i<=NF;i++) if($i=="Accepted"){print $(i+1), $(i+3), $(i+5)}}' | sort | uniq -c | sort -rn | head -n 25
	pw_logins="$(zgrep -hcE 'Accepted (password|keyboard-interactive)' /var/log/auth.log* /var/log/secure* 2>/dev/null | awk '{s += $1} END {print s + 0}')"
	((${pw_logins:-0} > 0)) && flag "$pw_logins successful PASSWORD login(s) in the logs - check who"
	echo "failed password guesses: $(zgrep -hE 'Failed password|Invalid user' /var/log/auth.log* /var/log/secure* 2>/dev/null | wc -l)"

	section "Accounts"
	echo "accounts that can log in (have a real shell), plus any uid 0:"
	awk -F: '$3 == 0 || $7 !~ /(nologin|false|sync|shutdown|halt)$/ {printf "  %-14s uid=%-6s %-22s %s\n", $1, $3, $6, $7}' /etc/passwd
	extra_root="$(awk -F: '$3 == 0 && $1 != "root" {print $1}' /etc/passwd)"
	[[ -n $extra_root ]] && flag "extra account(s) with root's uid 0: $extra_root"
	for g in sudo admin wheel; do getent group "$g"; done
	echo "sudoers rules:"; grep -rhE '^[^#[:space:]]' /etc/sudoers /etc/sudoers.d/ 2>/dev/null | sed 's/^/  /'
	nopw="$(grep -rhE '^[^#].*NOPASSWD' /etc/sudoers /etc/sudoers.d/ 2>/dev/null | grep -vE '^root[[:space:]]' || true)"
	[[ -n $nopw ]] && flag "sudo rules with NOPASSWD for someone other than root: $(tr '\n' ';' <<<"$nopw")"
	echo; echo "SSH keys that can log in:"
	while IFS=: read -r user _ _ _ _ home _; do
		ak="$home/.ssh/authorized_keys"
		[[ -f $ak ]] || continue
		echo "  -- $user ($ak)"
		ssh-keygen -lf "$ak" 2>/dev/null | sed 's/^/     /' || echo "     (couldn't read keys)"
	done </etc/passwd

	section "Firewall"
	if has ufw; then
		ufw status verbose 2>/dev/null
		ufw_st="$(ufw status 2>/dev/null || true)"
		grep -q 'Status: active' <<<"$ufw_st" || flag "ufw firewall is not active"
	else
		echo "ufw not installed"
		has nft && nft list ruleset 2>/dev/null | head -n 30
		flag "no ufw firewall (check for another firewall above, or a DigitalOcean cloud firewall)"
	fi

	section "Ports open to the network (not just this machine)"
	ss -Htlnup 2>/dev/null | awk '$5 !~ /^(127\.|\[::1\]|::1)/' | sed 's/^/  /'
	if has docker; then
		echo; echo "Docker containers and published ports (these bypass ufw!):"
		docker ps --format '  {{.Names}}  {{.Ports}}' 2>/dev/null
		dports="$(docker ps --format '{{.Ports}}' 2>/dev/null || true)"
		grep -qE '0\.0\.0\.0:|:::' <<<"$dports" &&
			flag "Docker publishes ports on all interfaces - Docker bypasses ufw for these"
	fi

	section "Web servers"
	if systemd_up; then
		enabled_web=()
		for s in nginx caddy apache2 httpd lighttpd; do
			st="$(systemctl is-enabled "$s" 2>/dev/null)" || true
			[[ -z $st || $st == *"not-found"* ]] && continue
			echo "$s: enabled=$st active=$(systemctl is-active "$s" 2>/dev/null)"
			[[ $st == enabled ]] && enabled_web+=("$s")
		done
		((${#enabled_web[@]} > 1)) && flag "more than one web server starts at boot (${enabled_web[*]}) - they race for ports 80/443"
	fi
	echo "holding ports 80/443:"; ss -Htlnp '( sport = :80 or sport = :443 )' 2>/dev/null | sed 's/^/  /'

	section "Certificates (certbot)"
	if has certbot; then
		certbot certificates 2>/dev/null | grep -E 'Certificate Name|Expiry Date' | sed 's/^ */  /'
		certs_out="$(certbot certificates 2>/dev/null || true)"
		grep -q 'INVALID' <<<"$certs_out" && flag "certbot has expired/invalid certificates (renewals will keep failing)"
	else
		echo "certbot not installed"
	fi

	section "Scheduled cron jobs"
	find /etc/cron.d -maxdepth 1 -type f -printf '  cron.d: %f\n' 2>/dev/null | sort
	while IFS=: read -r user _; do
		crontab -l -u "$user" 2>/dev/null | grep -vE '^[[:space:]]*(#|$)' | sed "s/^/  $user: /"
	done </etc/passwd

	section "FLAG SUMMARY ($HOST)"
	if ((${#FLAGS[@]})); then
		printf '  - %s\n' "${FLAGS[@]}"
	else
		echo "  nothing flagged"
	fi
}

audit >"$REPORT" 2>&1
chmod 600 "$REPORT"
[[ $OWNER != root ]] && chown "$OWNER" "$REPORT"

sed -n '/==== FLAG SUMMARY/,$p' "$REPORT"
echo
echo "Full report: $REPORT"
