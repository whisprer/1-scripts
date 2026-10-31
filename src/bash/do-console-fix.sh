#!/usr/bin/env bash
# do-console-fix.sh - make DigitalOcean's web Droplet Console work with our SSH on port 2357.
#
#   sudo bash ~/do-console-fix.sh           check, then (on yes) tell droplet-agent the real port
#   sudo bash ~/do-console-fix.sh --undo    remove that again
#
# Why: the console goes through DigitalOcean's droplet-agent, which uses the first Port line in
# /etc/ssh/sshd_config, or 22 if there is none. Our port (2357) is set in sshd_config.d, so the
# agent tries 22 and the console says "ssh config failed". The agent's documented option
# -sshd_port fixes that. This adds it through a drop-in
# (/etc/systemd/system/droplet-agent.service.d/10-sshd-port.conf) so package updates don't undo it.
# sshd itself is never touched - your SSH logins can't be affected.

if [ -z "${BASH_VERSION:-}" ]; then
	echo "Run this with bash:  sudo bash $0"
	exit 1
fi
set -uo pipefail

AGENT="droplet-agent.service"
DROPDIR="/etc/systemd/system/$AGENT.d"
DROPIN="$DROPDIR/10-sshd-port.conf"

pass() { printf '  PASS  %s\n' "$*"; }
warn() { printf '  WARN  %s\n' "$*"; }
fail() { printf '  FAIL  %s\n' "$*"; }
info() { printf '  ....  %s\n' "$*"; }
die() {
	fail "$*"
	exit 1
}
ask_yes() {
	local a=""
	read -r -p "  $1 " a || a=""
	[[ $a == yes ]]
}

[[ $EUID -eq 0 ]] || {
	echo "Run this with sudo:  sudo bash $0 ${1:-}"
	exit 1
}
HOST="$(hostname)"
printf '\n== %s: DigitalOcean web console ==\n' "$HOST"

agent_cmd() { # the running agent's command line
	local p
	p="$(systemctl show -p MainPID --value "$AGENT" 2>/dev/null)"
	[[ $p =~ ^[0-9]+$ && $p != 0 && -r /proc/$p/cmdline ]] && tr '\0' ' ' <"/proc/$p/cmdline"
}

if [[ ${1:-} == --undo ]]; then
	[[ -f $DROPIN ]] || die "nothing to undo on $HOST"
	rm -f -- "$DROPIN"
	rmdir --ignore-fail-on-non-empty "$DROPDIR" 2>/dev/null || true
	systemctl daemon-reload
	systemctl restart "$AGENT"
	pass "removed; droplet-agent is $(systemctl is-active "$AGENT") again with its own settings"
	exit 0
fi

# --- what SSH really listens on (read only)
SSHD_PORT="$(sshd -T 2>/dev/null | awk '$1 == "port" {print $2; exit}')"
[[ $SSHD_PORT =~ ^[0-9]+$ ]] || die "couldn't read sshd's port (sshd -T) - stopping"
info "SSH listens on port $SSHD_PORT"
ROOTLOGIN="$(sshd -T 2>/dev/null | awk '$1 == "permitrootlogin" {print $2; exit}')"

# --- the agent
if ! systemctl cat "$AGENT" >/dev/null 2>&1; then
	info "droplet-agent isn't installed here, so the web console can't work on this server."
	info "DigitalOcean's install line: curl -fsSL https://repos-droplet.digitalocean.com/install.sh | sudo bash"
	exit 0
fi
info "droplet-agent: $(systemctl is-active "$AGENT"), running: $(agent_cmd | cut -c1-120)"
MAINPORT="$(awk 'tolower($1) == "port" {print $2; exit}' /etc/ssh/sshd_config 2>/dev/null)"
CMD="$(agent_cmd)"
if [[ $CMD =~ -sshd_port[=\ ]+([0-9]+) ]]; then
	AGENT_PORT="${BASH_REMATCH[1]} (set by -sshd_port)"
	have="${BASH_REMATCH[1]}"
else
	have="${MAINPORT:-22}"
	AGENT_PORT="$have (from $([[ -n $MAINPORT ]] && echo "the Port line in sshd_config" || echo "its default - sshd_config has no Port line"))"
fi
info "droplet-agent sends the console to port $AGENT_PORT"
if [[ $have == "$SSHD_PORT" ]]; then
	pass "they match - the console's port is right"
	NEED=0
else
	warn "mismatch: the console goes to $have but SSH listens on $SSHD_PORT - that's the 'ssh config failed'"
	NEED=1
fi
[[ $ROOTLOGIN == no ]] && info "root can't log in over SSH here, so in the console set 'Log in as' to your own user (e.g. wofl), not root"
((NEED)) || exit 0

# --- the drop-in: same command the unit runs, plus -sshd_port
EXEC="$(systemctl cat "$AGENT" 2>/dev/null | grep -E '^[[:space:]]*ExecStart=.+' | grep -v 'sshd_port' | tail -n1 | sed -E 's/^[[:space:]]*ExecStart=//')"
[[ -n $EXEC ]] || die "couldn't read droplet-agent's start line - stopping, paste this to Claude"
EXEC="$(sed -E 's/[[:space:]]+-sshd_port[= ]+[0-9]+//' <<<"$EXEC")"
info "This adds $DROPIN:"
printf '        [Service]\n        ExecStart=\n        ExecStart=%s -sshd_port=%s\n' "$EXEC" "$SSHD_PORT"
info "then restarts droplet-agent (a second or two; SSH and your sites aren't involved)."
ask_yes "Type yes to tell droplet-agent the real SSH port:" || {
	info "nothing changed"
	exit 0
}
mkdir -p "$DROPDIR"
printf '# Added by do-console-fix.sh %s: our sshd listens on %s, set in sshd_config.d where the agent does not look.\n[Service]\nExecStart=\nExecStart=%s -sshd_port=%s\n' \
	"$(date '+%Y-%m-%d')" "$SSHD_PORT" "$EXEC" "$SSHD_PORT" >"$DROPIN"
chmod 644 "$DROPIN"
systemctl daemon-reload
systemctl restart "$AGENT"
sleep 2
if [[ "$(systemctl is-active "$AGENT")" == active && "$(agent_cmd)" == *"-sshd_port=$SSHD_PORT"* ]]; then
	pass "droplet-agent now sends the console to port $SSHD_PORT"
else
	fail "droplet-agent didn't come back with the new setting - removing it again"
	rm -f -- "$DROPIN"
	systemctl daemon-reload
	systemctl restart "$AGENT"
	die "put back ($(systemctl is-active "$AGENT")). Paste this to Claude."
fi
info "Try it: DigitalOcean dashboard > this Droplet > Access > Launch Droplet Console, 'Log in as' your own user."
