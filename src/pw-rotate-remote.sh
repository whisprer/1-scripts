#!/usr/bin/env bash
# pw-rotate-remote.sh - server half of rotate-passwords.ps1. Don't run it by hand.
# Reads three lines from stdin (never from the command line, so they can't show up in ps or logs):
#   1. your current password (for sudo)   2. your new password   3. root's new password
# One sudo call does the change, checks the stored hashes really match, and puts your old
# password back if yours didn't take - so you can't lose sudo. Then it proves sudo accepts the
# new password.
set -uo pipefail
umask 077

rd() { # one line from stdin, without a trailing CR (PowerShell may add one)
	local l=""
	IFS= read -r l || true
	printf '%s' "${l%$'\r'}"
}
CUR="$(rd)"
NEW="$(rd)"
NEWROOT="$(rd)"
ME="$(id -un)"
HOST="$(hostname)"
say() { printf '  %-10s %s\n' "$HOST" "$*"; }
fail() {
	say "FAIL  $*"
	exit 1
}

[[ $ME != root ]] || fail "run as your own user, not root"
[[ -n $CUR && -n $NEW && -n $NEWROOT ]] || fail "didn't receive all three passwords - nothing changed"
for p in "$NEW" "$NEWROOT"; do
	((${#p} >= 16)) || fail "a new password is shorter than 16 characters - nothing changed"
done
command -v perl >/dev/null 2>&1 || fail "perl is missing, so the new passwords couldn't be checked - nothing changed"

# 1. does the current password work for sudo?
if ! printf '%s\n' "$CUR" | sudo -k -S -p '' -v 2>/dev/null; then
	fail "the current password for $ME was refused by sudo - nothing changed"
fi
say "current password OK"

# 2. the change, the check and any put-back, all inside ONE sudo call. sudo reads the first line
#    as its password; the root part reads the next three.
ROOT_PART='
set -uo pipefail
ME=$1
IFS= read -r CUR; IFS= read -r NEW; IFS= read -r NEWROOT
[[ -n $CUR && -n $NEW && -n $NEWROOT ]] || { echo "FAIL  the passwords did not reach the root step - nothing changed"; exit 1; }
matches() { local h; h=$(getent shadow "$1" | cut -d: -f2); [[ -n $h ]] || return 1
	PW=$2 H=$h perl -e "exit(crypt(\$ENV{PW}, \$ENV{H}) eq \$ENV{H} ? 0 : 1)"; }
if ! printf "%s:%s\n%s:%s\n" "$ME" "$NEW" root "$NEWROOT" | chpasswd; then
	echo "FAIL  chpasswd refused the change - nothing changed"; exit 1
fi
if ! matches "$ME" "$NEW"; then
	printf "%s:%s\n" "$ME" "$CUR" | chpasswd
	if matches "$ME" "$CUR"; then echo "FAIL  your new password did not store - your OLD one is back (root may have changed)"; exit 1; fi
	echo "FAIL  your new password did not store and putting the old one back failed - use the DigitalOcean Recovery Console"; exit 1
fi
if ! matches root "$NEWROOT"; then echo "WARN  your password changed, but root did not verify - rerun this server"; exit 4; fi
echo "OK"
'
out="$(printf '%s\n%s\n%s\n%s\n' "$CUR" "$CUR" "$NEW" "$NEWROOT" | sudo -k -S -p '' /bin/bash -c "$ROOT_PART" rotate "$ME" 2>&1)"
rc=$?
if ((rc != 0)) || [[ $out != *OK* ]]; then
	say "${out:-FAIL  sudo step failed (exit $rc) - check this server by hand}"
	exit $((rc == 0 ? 1 : rc))
fi

# 3. the real proof for you: sudo itself accepts the new password
if printf '%s\n' "$NEW" | sudo -k -S -p '' -v 2>/dev/null; then
	sudo -k
	say "PASS  both passwords changed and verified; sudo accepts your new one"
	exit 0
fi
fail "the new password is stored but sudo didn't accept it - check this server by hand (the old password is NOT back)"
