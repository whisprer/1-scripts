#!/usr/bin/env python3
"""
repo_sync.py - keep GitHub, the P52 and the NAS in agreement.

    GitHub  = the truth
    P52     = where new work starts   (D:\\code)
    NAS     = an exact mirror of GitHub  (Z:\\GitHub-Repos)

One run = PLAN -> you type YES -> APPLY.  Nothing changes before the YES
(the plan only refreshes origin/* tracking refs so it can see what's new).

What happens to each repo:
    on GitHub, missing locally ........ clone to P52 and NAS
    P52 has new commits / branches .... push them to GitHub (NAS then pulls)
    GitHub has new commits ............ fast-forward P52 (never touches your
                                        uncommitted files) and the NAS
    NAS has anything extra ............ back it up once, reset NAS to GitHub
    moved / renamed on GitHub ......... fix the origin URL
    deleted on GitHub ................. back it up once, delete locally
                                        (brakes on if more than 10 at once)
    never been on GitHub .............. create a PRIVATE repo, push, mirror
    old-script README/doc stamps ...... commit + push (only when that's the
                                        ONLY uncommitted change)
    diverged / conflicts .............. NEEDS YOU list, never auto-merged

Backups: each state is fingerprinted and only zipped if it's new, into
<backup-root>/<run-time>/, with manifest.jsonl as the index.

Extra command:
    repo_sync.py dupes --root <folder> [--root ...] [--apply]
        finds copies of repos that hold nothing the live repo doesn't
        already have (identical or fully contained), and deletes them with
        --apply.

Needs: Python 3.10+ (stdlib only), Git 2.30+ on PATH, a GitHub token
(env GITHUB_PAT / GITHUB_TOKEN / GH_TOKEN, or `gh auth login`, or Git
Credential Manager).  Classic PAT scopes: repo, read:org.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional

TOOL_VERSION = "2.0.0"

# ---------------------------------------------------------------------------
# CONFIG - edit these defaults, or override on the command line
# ---------------------------------------------------------------------------

DEFAULT_P52_BASE = r"D:\code"
DEFAULT_NAS_BASE = r"Z:\GitHub-Repos"
DEFAULT_BACKUP_ROOT = r"Z:\_repo-sync-backups"
DEFAULT_DOCS_BASE = r"D:\code\1-git-docs"   # where the old stamp templates live
DEFAULT_JOBS = 8
DEFAULT_MAX_DELETES = 10                    # repo deletions per run before brakes

# Accounts/orgs that count as "yours". Empty = you + every org /user/orgs lists.
OWNERS: list[str] = []
# fnmatch patterns ("owner/name", "local:folder", or a folder name) to skip.
IGNORE: list[str] = []

API_URL = os.environ.get("REPO_SYNC_API_URL", "https://api.github.com").rstrip("/")
WEB_BASE = os.environ.get("REPO_SYNC_WEB_BASE", "https://github.com").rstrip("/")
API_VERSION = "2022-11-28"   # supported until at least 2028; also the default

# Folders under a base that are never treated as repos.
SKIP_DIR_NAMES = {
    ".repo-convergence-archive", "_repo-sync-backups", "$RECYCLE.BIN",
    "System Volume Information", "#recycle", "@eaDir", "@Recycle", ".recycle",
    ".@__thumb", ".Trash-1000", "lost+found",
}
TOMBSTONE_SUFFIX = ".__repo-sync-deleting__"

# Regenerable build/cache folders left out of full backups and fingerprints.
CACHE_DIR_NAMES = {
    "node_modules", "target", ".venv", "venv", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", ".tox", ".nox", ".gradle", ".next", ".nuxt",
    ".turbo", ".parcel-cache", ".cache", "zig-cache", ".zig-cache", ".vs",
    "cmake-build-debug", "cmake-build-release",
}

# The old repo_convergence.py stamps.
STAMP_DOCS = ["CHANGELOG.md", "CODE_OF_CONDUCT.md", "CONTRIBUTING.md", "LICENSE.md", "SECURITY.md"]
README_V2_START = "<!-- repo-convergence:readme-header:start -->"
README_V2_END = "<!-- repo-convergence:readme-header:end -->"
README_V1_MARKER = "[README.md]"
STAMP_COMMIT_MESSAGE = "docs: add standard README header and project docs"

def _mb_env(name: str, default_mb: float) -> int:
    try:
        return int(float(os.environ.get(name, default_mb)) * 1024 * 1024)
    except ValueError:
        return int(default_mb * 1024 * 1024)


# P52 work-in-progress snapshots only (nothing is destroyed there): bigger files are
# listed, not zipped. Backups taken before a NAS reset or a delete are never capped.
WIP_MAX_FILE_BYTES = _mb_env("REPO_SYNC_WIP_MAX_FILE_MB", 100)
WIP_MAX_TOTAL_BYTES = _mb_env("REPO_SYNC_WIP_MAX_TOTAL_MB", 1024)

GIT_TIMEOUT_LOCAL = 600
GIT_TIMEOUT_NET = 1800
GIT_TIMEOUT_CLONE = 7200

IS_WINDOWS = os.name == "nt"

P52 = "P52"
NAS = "NAS"
BASES = (P52, NAS)

# Identity kinds
ONLINE = "online"          # a repo you own on GitHub (listed or found via redirect)
FOREIGN = "foreign"        # someone else's repo / another git host: pull only
GONE = "gone"              # was on your GitHub, deleted there
LOCAL_ONLY = "local-only"  # never been online
SKIPPED = "skipped"        # can't be handled safely; reported

# ---------------------------------------------------------------------------
# SMALL UTILITIES
# ---------------------------------------------------------------------------

_print_lock = threading.Lock()


def out(msg: str = "") -> None:
    with _print_lock:
        print(msg, flush=True)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def run_stamp() -> str:
    return utc_now().strftime("%Y-%m-%d_%H%M%SZ")


def longpath(p: Path | str) -> str:
    """Windows extended-length path so deep node_modules etc. don't hit MAX_PATH."""
    s = str(p)
    if not IS_WINDOWS:
        return s
    s = os.path.abspath(s)
    if s.startswith("\\\\?\\"):
        return s
    if s.startswith("\\\\"):
        return "\\\\?\\UNC\\" + s[2:]
    return "\\\\?\\" + s


def redact_url(url: Optional[str]) -> str:
    """Hide any password/token embedded in a remote URL before printing it."""
    if not url:
        return ""

    def repl(m: re.Match) -> str:
        user, pw = m.group(1), m.group(2)
        if pw is not None:
            return f"://{user}:***@"
        if re.match(r"^(gh[pousr]_|github_pat_)", user) or len(user) > 39:
            return "://***@"
        return m.group(0)

    return re.sub(r"://([^/@:]+)(?::([^/@]*))?@", repl, url)


def to_kebab(name: str) -> str:
    name = re.sub(r"\.git$", "", name, flags=re.IGNORECASE)
    name = re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", name)
    name = re.sub(r"[_\s]+", "-", name)
    name = re.sub(r"[^a-zA-Z0-9\-]", "-", name)
    name = re.sub(r"-{2,}", "-", name)
    return name.lower().strip("-")


_WIN_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}


def safe_folder_name(name: str) -> str:
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", name).rstrip(". ")
    if not s:
        s = "repo"
    if s.split(".")[0].upper() in _WIN_RESERVED:
        s = s + "-repo"
    return s


def github_repo_name(name: str) -> str:
    """GitHub allows [A-Za-z0-9._-], max 100 chars."""
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-")
    s = re.sub(r"-{2,}", "-", s)
    if s in ("", ".", ".."):
        s = "repo"
    return s[:100]


def slug(s: str, limit: int = 90) -> str:
    x = re.sub(r"[^A-Za-z0-9._-]+", "_", s).strip("_") or "x"
    if len(x) > limit:
        x = x[: limit - 9] + "_" + hashlib.sha256(s.encode()).hexdigest()[:8]
    return x


def sha256_file(path: Path | str) -> str:
    h = hashlib.sha256()
    with open(longpath(path), "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def human_bytes(n: int) -> str:
    f = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if f < 1024 or unit == "TB":
            return f"{f:.0f} {unit}" if unit == "B" else f"{f:.1f} {unit}"
        f /= 1024
    return f"{n} B"


def _clear_readonly_and_retry(func: Callable, path: str, _exc: object) -> None:
    # Git marks pack/object files read-only on Windows; rmtree can't delete them otherwise.
    os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
    func(path)


def rmtree_safe(path: Path | str) -> None:
    target = longpath(path)
    if sys.version_info >= (3, 12):
        shutil.rmtree(target, onexc=_clear_readonly_and_retry)
    else:
        shutil.rmtree(target, onerror=_clear_readonly_and_retry)


def delete_repo_folder(path: Path) -> tuple[bool, str]:
    """Rename to a tombstone first (fails fast if something has the folder open),
    then delete. A half-deleted tombstone is never mistaken for a live repo."""
    tomb = path.with_name(path.name + TOMBSTONE_SUFFIX)
    n = 1
    while os.path.lexists(longpath(tomb)):
        n += 1
        tomb = path.with_name(f"{path.name}{TOMBSTONE_SUFFIX}{n}")
    try:
        os.rename(longpath(path), longpath(tomb))
    except OSError as e:
        return False, f"folder is in use or locked, not deleted ({e.strerror or e})"
    try:
        rmtree_safe(tomb)
    except OSError as e:
        return False, f"partly deleted; leftover folder {tomb} ({e.strerror or e})"
    return True, ""


def iter_worktree_files(root: Path) -> Iterable[tuple[str, Path]]:
    """(relpath with /, absolute path) for every file under root except .git
    internals and cache folders. Symlinks are listed, not followed."""
    root_s = longpath(root)
    for dirpath, dirnames, filenames in os.walk(root_s):
        rel_dir = os.path.relpath(dirpath, root_s)
        if rel_dir == ".":
            dirnames[:] = [d for d in dirnames if d != ".git" and d not in CACHE_DIR_NAMES]
        else:
            dirnames[:] = [d for d in dirnames if d not in CACHE_DIR_NAMES]
        dirnames.sort()
        for fn in sorted(filenames):
            full = os.path.join(dirpath, fn)
            rel = os.path.normpath(os.path.join(rel_dir, fn)).replace("\\", "/")
            yield rel, Path(full)


# ---------------------------------------------------------------------------
# GIT RUNNER
# ---------------------------------------------------------------------------


@dataclass
class GitResult:
    rc: int
    out: str
    err: str

    @property
    def ok(self) -> bool:
        return self.rc == 0

    def why(self) -> str:
        text = (self.err or self.out).strip()
        lines = [ln for ln in text.splitlines() if ln.strip()]
        keep = [ln for ln in lines if not ln.startswith("hint:")] or lines
        return " | ".join(keep[-3:])[:400] or f"exit {self.rc}"


def parse_safe_directory_hint(err: str) -> Optional[str]:
    """Git's 'dubious ownership' error tells us the exact safe.directory value
    to use (Git for Windows adds %(prefix)/ for UNC paths). Use it verbatim."""
    m = re.search(r"safe\.directory\s+(\S.*?)\s*$", err, re.M)
    if m:
        raw = m.group(1).strip()
        try:
            parts = shlex.split(raw, posix=True)
            if len(parts) == 1:
                return parts[0]
        except ValueError:
            pass
        return raw.strip("'\"")
    m = re.search(r"dubious ownership in repository at '(.*)'\s*$", err, re.M)
    if m:
        p = m.group(1)
        return ("%(prefix)/" + p) if (IS_WINDOWS and p.startswith("//")) else p
    return None


def find_git() -> str:
    candidates = [
        os.environ.get("REPO_SYNC_GIT", "").strip(),
        shutil.which("git") or "",
        r"C:\Program Files\Git\cmd\git.exe",
        r"C:\Program Files\Git\bin\git.exe",
    ]
    for c in candidates:
        if c and Path(c).exists():
            return c
    raise SystemExit("Could not find git. Install Git for Windows or set REPO_SYNC_GIT.")


class Git:
    def __init__(self, exe: str) -> None:
        self.exe = exe
        self._safe: dict[str, str] = {}
        self._probed: set[str] = set()
        self._lock = threading.Lock()
        self.base = ["-c", "core.quotepath=off", "-c", "color.ui=false",
                     "-c", "advice.detachedHead=false", "-c", "core.longpaths=true",
                     "-c", "gc.auto=0"]
        if IS_WINDOWS:
            # NTFS/SMB have no exec bit: a repo cloned on Linux would otherwise look
            # "modified" on every script file, forever.
            self.base += ["-c", "core.filemode=false"]
        env = os.environ.copy()
        env["GIT_TERMINAL_PROMPT"] = "0"     # never hang waiting for a password
        env["GCM_INTERACTIVE"] = "never"     # Git Credential Manager: no pop-ups mid-run
        env["LC_ALL"] = "C"                  # English messages (we parse a few)
        env.pop("GIT_DIR", None)
        env.pop("GIT_WORK_TREE", None)
        self.env = env
        self.interactive_env = dict(env)
        self.interactive_env.pop("GCM_INTERACTIVE", None)
        self.interactive_env.pop("GIT_TERMINAL_PROMPT", None)

    def _exec(self, cmd: list[str], timeout: int, input_text: Optional[str], env: dict) -> GitResult:
        try:
            p = subprocess.run(
                cmd,
                input=input_text.encode("utf-8") if input_text is not None else None,
                stdin=None if input_text is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=timeout, env=env,
            )
        except subprocess.TimeoutExpired:
            return GitResult(124, "", f"git timed out after {timeout}s: {' '.join(cmd[-4:])}")
        except OSError as e:
            return GitResult(127, "", f"could not run git: {e}")
        return GitResult(p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace"))

    def run(self, repo: Optional[Path], *args: str, timeout: int = GIT_TIMEOUT_LOCAL,
            input_text: Optional[str] = None, interactive: bool = False) -> GitResult:
        if repo is not None:
            key = str(repo)
            with self._lock:
                first = key not in self._probed
                self._probed.add(key)
            if first:
                # Some commands (git config, git bundle) silently act as if there's no repo
                # when ownership looks "dubious" (NAS shares); a strict command up front makes
                # git say so, and the safe.directory exception is remembered for this path.
                self._run(repo, ("rev-parse", "--git-dir"), GIT_TIMEOUT_LOCAL, None, False)
        return self._run(repo, args, timeout, input_text, interactive)

    def _run(self, repo: Optional[Path], args: tuple, timeout: int, input_text: Optional[str],
             interactive: bool) -> GitResult:
        env = self.interactive_env if interactive else self.env
        key = str(repo) if repo is not None else None

        def build(safe: Optional[str]) -> list[str]:
            cmd = [self.exe] + self.base
            if safe:
                cmd += ["-c", f"safe.directory={safe}"]
            if repo is not None:
                cmd += ["-C", str(repo)]
            return cmd + list(args)

        with self._lock:
            safe = self._safe.get(key) if key else None
        res = self._exec(build(safe), timeout, input_text, env)
        if res.rc != 0 and key and not safe and "detected dubious ownership" in res.err:
            hint = parse_safe_directory_hint(res.err)
            if hint:
                with self._lock:
                    self._safe[key] = hint
                res = self._exec(build(hint), timeout, input_text, env)
        return res

    def version(self) -> tuple[int, ...]:
        r = self.run(None, "--version")
        m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", r.out)
        if not m:
            return (0,)
        return tuple(int(x) for x in m.groups() if x is not None)


# ---------------------------------------------------------------------------
# REMOTE URL PARSING
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RemoteUrl:
    raw: str
    kind: str                  # "github" | "other" | "local"
    style: str                 # "https" | "scp" | "ssh" | "git" | "other"
    host: str = ""
    user: str = ""
    port: str = ""
    owner: str = ""
    name: str = ""

    @property
    def key(self) -> str:
        return f"{self.owner.lower()}/{self.name.lower()}"

    def norm(self) -> str:
        if self.kind == "local":
            return "local:" + self.raw.replace("\\", "/").rstrip("/").lower()
        if not self.host or not self.name:
            # couldn't take it apart: the whole URL is the identity (never lump unrelated ones)
            return "raw:" + re.sub(r"(\.git)?/*$", "", self.raw.strip().lower())
        host = self.host.lower()
        path = f"{self.owner}/{self.name}" if self.owner else self.name
        return f"{host}/{path}".lower()

    def with_repo(self, owner: str, name: str) -> str:
        """Same protocol/host/user as the original URL, new owner/name."""
        userinfo = f"{self.user}@" if self.user else ""
        if self.style == "scp":
            return f"{userinfo}{self.host}:{owner}/{name}.git"
        if self.style in ("ssh", "git"):
            port = f":{self.port}" if self.port else ""
            scheme = "ssh" if self.style == "ssh" else "git"
            return f"{scheme}://{userinfo}{self.host}{port}/{owner}/{name}.git"
        if self.style == "https" and self.kind == "github":
            scheme = "http" if self.raw.lower().startswith("http://") else "https"
            port = f":{self.port}" if self.port else ""
            return f"{scheme}://{userinfo}{self.host}{port}/{owner}/{name}.git"
        return f"{WEB_BASE}/{owner}/{name}.git"


_GITHUB_HOSTS = {"github.com", "www.github.com", "ssh.github.com"}
_ssh_alias_cache: dict[str, str] = {}
_ssh_alias_lock = threading.Lock()


def _ssh_binary() -> Optional[str]:
    found = shutil.which("ssh")
    if found:
        return found
    for c in (r"C:\Windows\System32\OpenSSH\ssh.exe", r"C:\Program Files\Git\usr\bin\ssh.exe"):
        if os.path.exists(c):
            return c
    return None


def _ssh_config_hostname(alias: str, cfg: Path) -> Optional[str]:
    """Minimal ~/.ssh/config reader (Host blocks + HostName) for when there's no ssh client."""
    try:
        text = cfg.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    a = alias.lower()
    active = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = re.split(r"\s*=\s*|\s+", line, maxsplit=1)
        key, val = parts[0].lower(), (parts[1] if len(parts) > 1 else "").strip()
        if key == "host":
            pats = val.lower().split()
            active = (any(fnmatch.fnmatch(a, pt) for pt in pats if not pt.startswith("!"))
                      and not any(fnmatch.fnmatch(a, pt[1:]) for pt in pats if pt.startswith("!")))
        elif key == "match":
            active = False
        elif active and key == "hostname":
            return val.replace("%h", alias).lower()
    return None


def resolve_ssh_host(alias: str) -> Optional[str]:
    """The real host an SSH alias from ~/.ssh/config points at (`ssh -G` reads the
    config, no network; without an ssh client the config file is read directly).
    None when that can't be worked out."""
    key = alias.lower()
    with _ssh_alias_lock:
        if key in _ssh_alias_cache:
            return _ssh_alias_cache[key] or None
    real = ""
    cfg = os.environ.get("REPO_SYNC_SSH_CONFIG", "").strip()
    ssh = _ssh_binary()
    if ssh:
        cmd = [ssh] + (["-F", cfg] if cfg else []) + ["-G", alias]
        try:
            pr = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                stdin=subprocess.DEVNULL, timeout=15)
            for line in pr.stdout.decode("utf-8", "replace").splitlines():
                if line.lower().startswith("hostname "):
                    real = line.split(None, 1)[1].strip().lower()
                    break
        except (OSError, subprocess.TimeoutExpired):
            real = ""
    if not real:
        real = _ssh_config_hostname(alias, Path(cfg) if cfg else Path.home() / ".ssh" / "config") or ""
    with _ssh_alias_lock:
        _ssh_alias_cache[key] = real
    return real or None


def _is_github_host(host: str, style: str) -> bool:
    h = host.lower()
    if h in _GITHUB_HOSTS:
        return True
    web_host = urllib.parse.urlparse(WEB_BASE).netloc.lower()
    if h == web_host:
        return True
    if style in ("scp", "ssh"):
        # SSH alias (e.g. "gh" or "github-work" in ~/.ssh/config): ask ssh where it really goes
        real = resolve_ssh_host(host)
        if real is not None:
            return real in _GITHUB_HOSTS
        return h.startswith("github")          # no ssh client to ask: best guess
    return False


def parse_remote_url(url: Optional[str]) -> Optional[RemoteUrl]:
    if not url:
        return None
    u = url.strip()
    if (re.match(r"^[A-Za-z]:[\\/]", u) or u.startswith("\\\\") or u.startswith("/")
            or u.startswith("./") or u.startswith("../") or u.lower().startswith("file://")):
        return RemoteUrl(raw=u, kind="local", style="other")
    m = re.match(r"^(?P<scheme>https?|ssh|git|git\+ssh|ssh\+git)://(?:(?P<user>[^@/]+)@)?"
                 r"(?P<host>[^/:]+)(?::(?P<port>\d+))?/(?P<path>.+?)/*$", u, re.I)
    if m:
        scheme = m.group("scheme").lower()
        style = "https" if scheme.startswith("http") else ("git" if scheme == "git" else "ssh")
        host, user, port, path = m.group("host"), m.group("user") or "", m.group("port") or "", m.group("path")
    else:
        m = re.match(r"^(?:(?P<user>[^@/]+)@)?(?P<host>[^:/]{2,}):(?P<path>.+?)/*$", u)
        if not m:
            return RemoteUrl(raw=u, kind="other", style="other")
        style, host, user, port, path = "scp", m.group("host"), m.group("user") or "", "", m.group("path")
    path = re.sub(r"\.git$", "", path, flags=re.I).strip("/")
    parts = [p for p in path.split("/") if p]
    if _is_github_host(host, style) and len(parts) == 2:
        return RemoteUrl(raw=u, kind="github", style=style, host=host, user=user, port=port,
                         owner=parts[0], name=parts[1])
    return RemoteUrl(raw=u, kind="other", style=style, host=host, user=user, port=port,
                     owner="/".join(parts[:-1]), name=parts[-1] if parts else "")


# ---------------------------------------------------------------------------
# GITHUB API (stdlib urllib; no requests needed)
# ---------------------------------------------------------------------------


class ApiError(Exception):
    def __init__(self, status: int, message: str, url: str = "") -> None:
        super().__init__(f"GitHub API {status}: {message}" + (f" ({url})" if url else ""))
        self.status = status
        self.message = message


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Redirects are handled by hand so the token is never sent to another host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        return None


def _api_msg(data: object) -> str:
    if isinstance(data, dict):
        msg = str(data.get("message") or "")
        errs = data.get("errors")
        if isinstance(errs, list) and errs:
            bits = []
            for e in errs[:3]:
                bits.append(str(e.get("message") or e.get("code") or e) if isinstance(e, dict) else str(e))
            msg += " (" + "; ".join(bits) + ")"
        return msg or "no message"
    return "no message"


def _next_link(link: Optional[str]) -> Optional[str]:
    if not link:
        return None
    for url, rel in re.findall(r'<([^>]+)>\s*;\s*rel="([^"]+)"', link):
        if rel == "next":
            return url
    return None


@dataclass
class OnlineRepo:
    owner: str
    name: str
    private: bool = True
    archived: bool = False
    disabled: bool = False
    fork: bool = False
    default_branch: Optional[str] = None

    @property
    def key(self) -> str:
        return f"{self.owner.lower()}/{self.name.lower()}"

    @property
    def full(self) -> str:
        return f"{self.owner}/{self.name}"

    @property
    def url(self) -> str:
        return f"{WEB_BASE}/{self.owner}/{self.name}.git"

    @staticmethod
    def from_api(d: dict) -> "OnlineRepo":
        return OnlineRepo(
            owner=str(d["owner"]["login"]), name=str(d["name"]),
            private=bool(d.get("private", True)), archived=bool(d.get("archived")),
            disabled=bool(d.get("disabled")), fork=bool(d.get("fork")),
            default_branch=d.get("default_branch") or None,
        )


def _q(s: str) -> str:
    return urllib.parse.quote(s, safe="")


class GitHubApi:
    def __init__(self, token: str, api_url: str = API_URL) -> None:
        self.token = token
        self.api = api_url.rstrip("/")
        self.host = urllib.parse.urlparse(self.api).netloc.lower()
        self.opener = urllib.request.build_opener(_NoRedirect())
        self._create_lock = threading.Lock()
        self._last_create = 0.0

    def _open(self, method: str, url: str, body: Optional[dict]):
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", API_VERSION)
        req.add_header("User-Agent", f"repo-sync/{TOOL_VERSION}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with self.opener.open(req, timeout=60) as r:
                return r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            try:
                raw = e.read()
            except Exception:
                raw = b""
            return e.code, e.headers, raw

    @staticmethod
    def _rate_limited(headers, raw: bytes) -> bool:
        if headers is None:
            return False
        if headers.get("Retry-After") or headers.get("X-RateLimit-Remaining") == "0":
            return True
        return b"rate limit" in (raw or b"").lower()

    @staticmethod
    def _wait_rate_limit(headers) -> None:
        wait = 60
        ra = headers.get("Retry-After") if headers else None
        reset = headers.get("X-RateLimit-Reset") if headers else None
        if ra and str(ra).isdigit():
            wait = int(ra)
        elif reset and str(reset).isdigit():
            wait = max(1, int(reset) - int(time.time()) + 2)
        if wait > 900:
            raise ApiError(429, f"rate limited for another {wait // 60} min - run again later")
        out(f"  (GitHub rate limit - waiting {wait}s)")
        time.sleep(wait)

    def request(self, method: str, path_or_url: str, body: Optional[dict] = None,
                params: Optional[dict] = None):
        url = path_or_url if path_or_url.startswith("http") else self.api + path_or_url
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        redirects = 0
        attempt = 0
        while True:
            try:
                status, headers, raw = self._open(method, url, body)
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
                attempt += 1
                if attempt <= 3:
                    time.sleep(2 ** attempt)
                    continue
                raise ApiError(0, f"network error: {e}", url) from None
            if status in (301, 302, 303, 307, 308):
                loc = headers.get("Location") if headers else None
                if not loc or redirects >= 5:
                    raise ApiError(status, "bad redirect", url)
                new_url = urllib.parse.urljoin(url, loc)
                if urllib.parse.urlparse(new_url).netloc.lower() != self.host:
                    raise ApiError(status, f"refusing redirect to another host ({new_url})", url)
                if status in (301, 302, 303) and method != "GET":
                    raise ApiError(status, "repository moved; not replaying a write", url)
                url, redirects = new_url, redirects + 1
                continue
            if status in (403, 429) and self._rate_limited(headers, raw):
                attempt += 1
                if attempt <= 4:
                    self._wait_rate_limit(headers)
                    continue
            if status in (500, 502, 503, 504):
                attempt += 1
                if attempt <= 3:
                    time.sleep(2 ** attempt)
                    continue
            data = None
            if raw:
                try:
                    data = json.loads(raw.decode("utf-8", "replace"))
                except ValueError:
                    data = None
            return status, headers, data

    def paginate(self, path: str, params: Optional[dict] = None) -> list:
        p = {"per_page": "100"}
        p.update(params or {})
        url: Optional[str] = self.api + path + "?" + urllib.parse.urlencode(p)
        items: list = []
        pages = 0
        while url:
            status, headers, data = self.request("GET", url)
            if status != 200:
                raise ApiError(status, _api_msg(data), url)
            if not isinstance(data, list):
                raise ApiError(status, "expected a list", url)
            items.extend(data)
            url = _next_link(headers.get("Link") if headers else None)
            pages += 1
            if pages > 2000:
                raise ApiError(0, "pagination runaway", path)
        return items

    def me(self) -> str:
        status, _, data = self.request("GET", "/user")
        if status == 401:
            raise SystemExit("GitHub rejected the token (401). Check it hasn't expired/been revoked.")
        if status != 200 or not isinstance(data, dict) or not data.get("login"):
            raise ApiError(status, _api_msg(data), "/user")
        return str(data["login"])

    def my_orgs(self) -> list[str]:
        return sorted({str(o["login"]) for o in self.paginate("/user/orgs") if o.get("login")}, key=str.lower)

    def list_repos(self, owner: str, me: str) -> list[OnlineRepo]:
        if owner.lower() == me.lower():
            recs = self.paginate("/user/repos", {"affiliation": "owner"})
        else:
            recs = self.paginate(f"/orgs/{_q(owner)}/repos", {"type": "all"})
        repos = []
        for d in recs:
            try:
                r = OnlineRepo.from_api(d)
            except (KeyError, TypeError):
                continue
            if r.owner.lower() == owner.lower():
                repos.append(r)
        return repos

    def get_repo(self, owner: str, name: str) -> tuple[int, Optional[OnlineRepo], str]:
        """Follows GitHub's redirect for transferred/renamed repos."""
        status, _, data = self.request("GET", f"/repos/{_q(owner)}/{_q(name)}")
        if status == 200 and isinstance(data, dict) and data.get("owner"):
            return 200, OnlineRepo.from_api(data), ""
        return status, None, _api_msg(data)

    def create_private_repo(self, owner: str, name: str, me: str, description: str) -> tuple[Optional[OnlineRepo], str]:
        with self._create_lock:   # pace creations: GitHub throttles bursts of content creation
            gap = time.time() - self._last_create
            if gap < 1.5:
                time.sleep(1.5 - gap)
            body = {"name": name, "private": True, "description": description[:350], "auto_init": False}
            path = "/user/repos" if owner.lower() == me.lower() else f"/orgs/{_q(owner)}/repos"
            status, _, data = self.request("POST", path, body=body)
            self._last_create = time.time()
        if status == 201 and isinstance(data, dict):
            return OnlineRepo.from_api(data), ""
        return None, f"{status} {_api_msg(data)}"

    def set_default_branch(self, owner: str, name: str, branch: str) -> tuple[bool, str]:
        status, _, data = self.request("PATCH", f"/repos/{_q(owner)}/{_q(name)}", body={"default_branch": branch})
        return (status == 200), ("" if status == 200 else f"{status} {_api_msg(data)}")


def find_token(git: Git) -> tuple[str, str]:
    for env_name in ("GITHUB_PAT", "GH_RENAME_PAT", "GITHUB_TOKEN", "GH_TOKEN"):
        v = os.environ.get(env_name, "").strip()
        if v:
            return v, f"env {env_name}"
    gh = shutil.which("gh")
    if gh:
        try:
            p = subprocess.run([gh, "auth", "token"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               stdin=subprocess.DEVNULL, timeout=30)
            tok = p.stdout.decode("utf-8", "replace").strip()
            if p.returncode == 0 and tok:
                return tok, "gh auth token"
        except (OSError, subprocess.TimeoutExpired):
            pass
    host = urllib.parse.urlparse(WEB_BASE).netloc or "github.com"
    r = git.run(None, "credential", "fill", input_text=f"protocol=https\nhost={host}\n\n", timeout=60)
    if r.ok:
        for line in r.out.splitlines():
            if line.startswith("password="):
                tok = line[len("password="):].strip()
                if tok:
                    return tok, "Git Credential Manager"
    raise SystemExit(
        "No GitHub token found.\n"
        "  Set one (PowerShell, persists for new windows):\n"
        "    [Environment]::SetEnvironmentVariable('GITHUB_PAT', '<token>', 'User')\n"
        "  Use a CLASSIC personal access token with scopes: repo, read:org\n"
        "  (fine-grained tokens can only see one account/org at a time)."
    )


# ---------------------------------------------------------------------------
# DATA MODEL
# ---------------------------------------------------------------------------


@dataclass
class Copy:
    base: str
    path: Path
    folder: str
    origin_url: Optional[str]
    remote: Optional[RemoteUrl]
    roots: Optional[frozenset] = None     # root commits, filled lazily

    @property
    def label(self) -> str:
        return f"{self.base}:{self.folder}"


@dataclass
class Issue:
    key: str
    base: Optional[str]
    kind: str
    message: str
    hint: str = ""


@dataclass
class Identity:
    key: str
    kind: str
    online: Optional[OnlineRepo] = None
    url: Optional[str] = None                     # URL to clone/fetch from
    copies: dict = field(default_factory=lambda: {P52: [], NAS: []})
    canonical: dict = field(default_factory=lambda: {P52: None, NAS: None})
    target: dict = field(default_factory=lambda: {P52: None, NAS: None})
    new_repo: Optional[tuple] = None              # (owner, name) to create, LOCAL_ONLY
    moved_from: set = field(default_factory=set)
    notes: list = field(default_factory=list)
    issues: list = field(default_factory=list)

    @property
    def display(self) -> str:
        if self.kind == ONLINE and self.online is not None:
            return self.online.full
        if self.kind == GONE:
            name = self.online.full if self.online is not None else self.key.split(":", 1)[1]
            return f"{name} (deleted on GitHub)"
        if self.kind == LOCAL_ONLY:
            base = self.key.split(":", 1)[1].split("#")[0]
            if self.new_repo and "/" in base:
                return f"{self.new_repo[0]}/{self.new_repo[1]} (never created on GitHub)"
            return f"{base} (never online)" + (f" -> {self.new_repo[0]}/{self.new_repo[1]}" if self.new_repo else "")
        if self.kind == FOREIGN:
            if self.online is not None:
                return f"{self.online.full} (not yours)"
            return self.key.split(":", 1)[1] + " (other host)"
        return self.key.split(":", 1)[-1]

    def all_copies(self) -> list[Copy]:
        return self.copies[P52] + self.copies[NAS]


# Steps that change something you'd care about: in the APPLY pass they only run
# if the PLAN pass showed the same step (same repo, place, and target).
GATED = {"create-online", "publish", "commit-stamps", "discard-stamps", "delete-repo", "delete-branch",
         "nas-reset", "rename-folder", "push", "push-new", "prune-refs"}


@dataclass
class Step:
    base: Optional[str]
    kind: str
    target: str
    detail: str
    status: str = "planned"      # planned | done | failed | skipped
    error: str = ""

    @property
    def sig(self) -> tuple:
        return (self.base or "", self.kind, self.target)


@dataclass
class Result:
    key: str
    display: str
    kind: str
    steps: list = field(default_factory=list)
    issues: list = field(default_factory=list)
    infos: list = field(default_factory=list)
    backups: list = field(default_factory=list)
    dirty: list = field(default_factory=list)     # places with uncommitted work (left alone)

    def step(self, base: Optional[str], kind: str, target: str, detail: str) -> Step:
        s = Step(base, kind, target, detail)
        self.steps.append(s)
        return s

    def issue(self, base: Optional[str], kind: str, message: str, hint: str = "") -> None:
        self.issues.append(Issue(self.key, base, kind, message, hint))

    def info(self, msg: str) -> None:
        self.infos.append(msg)


# ---------------------------------------------------------------------------
# REPO INSPECTION HELPERS
# ---------------------------------------------------------------------------


class RepoError(Exception):
    pass


IN_PROGRESS_MARKERS = {
    "MERGE_HEAD": "a merge", "rebase-merge": "a rebase", "rebase-apply": "a rebase/am",
    "CHERRY_PICK_HEAD": "a cherry-pick", "REVERT_HEAD": "a revert", "BISECT_LOG": "a bisect",
}


def in_progress(repo: Path) -> Optional[str]:
    g = repo / ".git"
    for name, what in IN_PROGRESS_MARKERS.items():
        if os.path.exists(longpath(g / name)):
            return what
    return None


def head_state(git: Git, repo: Path) -> tuple[Optional[str], Optional[str]]:
    """(current branch name or None when detached, HEAD sha or None when unborn)."""
    r = git.run(repo, "symbolic-ref", "-q", "HEAD")
    ref = r.out.strip()
    branch = ref[len("refs/heads/"):] if r.ok and ref.startswith("refs/heads/") else None
    s = git.run(repo, "rev-parse", "-q", "--verify", "HEAD^{commit}")
    sha = s.out.strip() if s.ok and s.out.strip() else None
    return branch, sha


@dataclass
class Branch:
    name: str
    sha: str
    upstream: str          # full ref, e.g. refs/remotes/origin/main ("" = none)
    upstream_remote: str   # "origin", another remote, "." or ""
    gone: bool


def list_branches(git: Git, repo: Path) -> list[Branch]:
    fmt = "%(refname)%00%(objectname)%00%(upstream)%00%(upstream:remotename)%00%(upstream:track)"
    r = git.run(repo, "for-each-ref", f"--format={fmt}", "refs/heads")
    if not r.ok:
        raise RepoError(f"could not list branches: {r.why()}")
    res = []
    for line in r.out.splitlines():
        parts = line.split("\x00")
        if len(parts) < 5 or not parts[0].startswith("refs/heads/"):
            continue
        res.append(Branch(parts[0][len("refs/heads/"):], parts[1], parts[2], parts[3], "[gone]" in parts[4]))
    return res


TRACKING_NS = "refs/remotes/origin"
PREVIEW_NS = "refs/repo-sync/preview"     # plan-pass look at a repo that isn't on GitHub yet


def origin_refs(git: Git, repo: Path, ns: str = TRACKING_NS) -> dict[str, str]:
    r = git.run(repo, "for-each-ref", "--format=%(refname)%00%(objectname)", ns)
    refs = {}
    for line in r.out.splitlines():
        name, _, sha = line.partition("\x00")
        short = name[len(ns) + 1:]
        if short and short != "HEAD":
            refs[short] = sha
    return refs


def drop_refs(git: Git, repo: Path, ns: str) -> None:
    for name in git.run(repo, "for-each-ref", "--format=%(refname)", ns).out.split():
        git.run(repo, "update-ref", "-d", name)


def ahead_behind(git: Git, repo: Path, left: str, right: str) -> tuple[int, int]:
    r = git.run(repo, "rev-list", "--left-right", "--count", f"{left}...{right}")
    parts = r.out.split()
    if not r.ok or len(parts) != 2:
        raise RepoError(f"could not compare {left} with {right}: {r.why()}")
    return int(parts[0]), int(parts[1])


def count_not_in(git: Git, repo: Path, ref, exclude: list[str]) -> int:
    """Commits reachable from ref (a ref or a list of refs/shas) but from none of
    exclude. -1 on error, which callers treat as 'unique, back it up'."""
    refs = [ref] if isinstance(ref, str) else list(ref)
    if not refs:
        return 0
    lines = refs + [f"^{x}" for x in exclude]
    r = git.run(repo, "rev-list", "--count", "--stdin", input_text="\n".join(lines) + "\n")
    try:
        return int(r.out.strip()) if r.ok else -1
    except ValueError:
        return -1


def ls_remote_refs(git: Git, repo: Optional[Path], remote: str) -> Optional[dict[str, str]]:
    """Every ref on the remote right now: {refname: sha}. None if it couldn't be reached."""
    r = git.run(repo, "ls-remote", remote, timeout=GIT_TIMEOUT_NET)
    if not r.ok:
        return None
    refs = {}
    for line in r.out.splitlines():
        sha, _, ref = line.partition("\t")
        if ref.startswith("refs/"):
            refs[ref.strip()] = sha.strip()
    return refs


@dataclass
class RemoteState:
    spec: str                 # "origin", a URL, or (preview) a local repo path
    live: dict                # branch -> sha, on GitHub now
    tracking: dict            # branch -> sha, tracking refs (ns/*) in this copy
    ns: str = TRACKING_NS     # refs/remotes/origin, or PREVIEW_NS for a preview

    def ref(self, branch: str) -> str:
        return f"{self.ns}/{branch}"

    @property
    def remotes(self) -> dict:
        return {k: v for k, v in self.tracking.items() if k in self.live}

    @property
    def stale(self) -> dict:
        return {k: v for k, v in self.tracking.items() if k not in self.live}

    @property
    def live_refs(self) -> list[str]:
        return [self.ref(k) for k in sorted(self.remotes)]


_SHA_RE = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")


def stash_shas(git: Git, repo: Path) -> list[str]:
    """Every stash entry (stash@{0}, stash@{1}, ...), not just the newest."""
    r = git.run(repo, "reflog", "show", "--format=%H", "refs/stash")
    return [x for x in r.out.split() if _SHA_RE.match(x)] if r.ok else []


def short_digest(*parts: object) -> str:
    h = hashlib.sha256()
    for part in parts:
        h.update(repr(part).encode("utf-8", "replace"))
        h.update(b"\x00")
    return h.hexdigest()[:12]


def ref_sha(git: Git, repo: Path, ref: str) -> Optional[str]:
    r = git.run(repo, "rev-parse", "-q", "--verify", ref)
    return r.out.strip() if r.ok and r.out.strip() else None


def has_any_ref(git: Git, repo: Path, pattern: str = "") -> bool:
    args = ["for-each-ref", "--count=1", "--format=%(refname)"]
    if pattern:
        args.append(pattern)
    r = git.run(repo, *args)
    return bool(r.ok and r.out.strip())


def root_commits(git: Git, repo: Path) -> frozenset:
    r = git.run(repo, "rev-list", "--max-parents=0", "--exclude=refs/repo-sync/*", "--all")
    return frozenset(r.out.split()) if r.ok else frozenset()


def other_worktree_branches(git: Git, repo: Path) -> set[str]:
    if not os.path.isdir(longpath(repo / ".git" / "worktrees")):
        return set()                                # no linked worktrees: skip the git call
    r = git.run(repo, "worktree", "list", "--porcelain")
    if not r.ok:
        return set()
    blocks = [b for b in r.out.replace("\r\n", "\n").split("\n\n") if b.strip()]
    names = set()
    for b in blocks[1:]:                       # the first block is this (main) worktree
        for line in b.splitlines():
            if line.startswith("branch refs/heads/"):
                names.add(line[len("branch refs/heads/"):])
    return names


@dataclass
class Entry:
    xy: str
    path: str


def status_entries(git: Git, repo: Path) -> list[Entry]:
    r = git.run(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all",
                "--ignored=no", "--no-renames")
    if not r.ok:
        raise RepoError(f"git status failed: {r.why()}")
    res = []
    for item in r.out.split("\x00"):
        if len(item) >= 4:
            res.append(Entry(item[:2], item[3:]))
    return res


def is_cache_path(rel: str) -> bool:
    return any(part in CACHE_DIR_NAMES for part in rel.replace("\\", "/").split("/"))


def detect_default_branch(git: Git, repo: Path, online: Optional[OnlineRepo]) -> Optional[str]:
    if online is not None and online.default_branch:
        return online.default_branch
    r = git.run(repo, "symbolic-ref", "-q", "refs/remotes/origin/HEAD")
    if r.ok and r.out.strip().startswith("refs/remotes/origin/"):
        return r.out.strip()[len("refs/remotes/origin/"):]
    r = git.run(repo, "ls-remote", "--symref", "origin", "HEAD", timeout=GIT_TIMEOUT_NET)
    m = re.search(r"^ref:\s+refs/heads/(\S+)\s+HEAD", r.out, re.M)
    if m:
        return m.group(1)
    refs = origin_refs(git, repo)
    for guess in ("main", "master"):
        if guess in refs:
            return guess
    return None


# ---- old-script stamp detection (ported from repo_convergence.py) ----------


def _norm_nl(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _strip_leading_h1(body: str, repo_name: str) -> str:
    body = body.lstrip("﻿\n")
    pattern = re.compile(rf"^#\s+{re.escape(repo_name)}\s*(?:\n+|$)", re.IGNORECASE)
    return pattern.sub("", body, count=1).lstrip("\n")


def _strip_old_header(existing: str, repo_name: str) -> str:
    text = _norm_nl(existing).lstrip("﻿")
    managed = re.compile(re.escape(README_V2_START) + r".*?" + re.escape(README_V2_END) + r"\s*",
                         flags=re.DOTALL | re.IGNORECASE)
    text = managed.sub("", text, count=1)
    if text.lstrip().startswith(README_V1_MARKER):
        lines = text.split("\n")
        if lines and lines[0].strip() == README_V1_MARKER:
            lines = lines[1:]
        text = "\n".join(lines).lstrip("\n")
        while True:
            p_block = re.match(r"^<p\s+align=\"center\">.*?</p>\s*", text, flags=re.DOTALL | re.IGNORECASE)
            if not p_block:
                break
            text = text[p_block.end():]
        body_lines = text.split("\n")
        idx = 0
        while idx < len(body_lines):
            line = body_lines[idx].strip()
            if not line or "shields.io" in line or line.startswith("![") or (
                    line.startswith("[") and "](https://github.com/" in line):
                idx += 1
                continue
            break
        text = "\n".join(body_lines[idx:]).lstrip("\n")
        if text.lower().startswith("<p align=\"center\">") and "/assets/" in text[:1000].lower():
            p_block = re.match(r"^<p\s+align=\"center\">.*?</p>\s*", text, flags=re.DOTALL | re.IGNORECASE)
            if p_block:
                text = text[p_block.end():]
    return _strip_leading_h1(text, repo_name).lstrip("\n")


def _old_default_body(repo: str) -> str:
    return (
        "## Overview\n\n"
        f"Describe what **{repo}** does, why it exists, and what problem it solves.\n\n"
        "## Status\n\n"
        "Document the current maturity, stability, and short roadmap.\n\n"
        "## Getting Started\n\n"
        "Add install, build, and usage instructions here.\n"
    )


def _canon(text: str) -> str:
    t = _norm_nl(text).lstrip("﻿")
    t = "\n".join(ln.rstrip() for ln in t.split("\n"))
    return re.sub(r"\n{3,}", "\n\n", t).strip()


def readme_is_pure_stamp(working: str, head: Optional[str], names: list[str]) -> bool:
    """True when the ONLY difference between HEAD's README and the working
    README is the old script's header (so committing/discarding loses nothing
    you wrote)."""
    if README_V2_START not in working and not working.lstrip("﻿ \r\n").startswith(README_V1_MARKER):
        return False
    for n in dict.fromkeys(names):
        if not n:
            continue
        wb = _canon(_strip_old_header(working, n))
        if head is None or not head.strip():
            if wb == "" or wb == _canon(_old_default_body(n)):
                return True
        elif wb == _canon(_strip_old_header(head, n)):
            return True
    return False


@dataclass
class StampInfo:
    kind: str                     # "none" | "stamp-only" | "mixed"
    paths: list = field(default_factory=list)
    templates_verified: bool = False


def detect_stamps(git: Git, repo: Path, entries: list[Entry], templates: dict[str, bytes],
                  names: list[str]) -> StampInfo:
    if not entries:
        return StampInfo("none")
    stamp_paths: list[str] = []
    other: list[str] = []
    readme: Optional[Entry] = None
    for e in entries:
        if e.path == "README.md" and "D" not in e.xy:
            readme = e
        elif e.path in STAMP_DOCS and e.xy == "??" and _same_as_template(repo / e.path, templates.get(e.path)):
            stamp_paths.append(e.path)      # byte-identical to the template the old script copied
        else:
            other.append(e.path)            # includes a LICENSE.md etc. you wrote yourself
    if readme is not None:
        try:
            with open(longpath(repo / "README.md"), encoding="utf-8", errors="replace") as f:
                working = f.read()
        except OSError:
            working = ""
        r = git.run(repo, "show", "HEAD:README.md")
        head = r.out if r.ok else None
        if readme_is_pure_stamp(working, head, names):
            stamp_paths.insert(0, "README.md")
        else:
            other.append("README.md")
    if not stamp_paths:
        return StampInfo("none")
    return StampInfo("mixed" if other else "stamp-only", stamp_paths, True)


def _same_as_template(path: Path, template: Optional[bytes]) -> bool:
    if template is None:
        return False
    try:
        with open(longpath(path), "rb") as f:
            return f.read() == template
    except OSError:
        return False


# ---------------------------------------------------------------------------
# FINGERPRINTS + BACKUP STORE
# ---------------------------------------------------------------------------


def _entry_digest(repo: Path, rel: str) -> str:
    p = repo / rel
    lp = longpath(p)
    try:
        if os.path.islink(lp):
            return "link:" + os.readlink(lp)
        if os.path.isdir(lp):
            return "dir"
        if not os.path.exists(lp):
            return "missing"
        return sha256_file(p)
    except OSError as ex:
        return f"err:{ex.errno}"


def fingerprint_extras(repo: Path, entries: list[Entry], extra_refs: list[tuple[str, str]]) -> str:
    h = hashlib.sha256(b"extras-v1\n")
    for name, sha in sorted(extra_refs):
        h.update(f"R {name} {sha}\n".encode("utf-8", "replace"))
    for e in sorted(entries, key=lambda x: x.path):
        h.update(f"E {e.xy} {e.path} {_entry_digest(repo, e.path)}\n".encode("utf-8", "replace"))
    return h.hexdigest()


def ignored_cache_dirs(git: Git, repo: Path) -> set[str]:
    """Folders git itself ignores AND that are well-known rebuildable caches
    (node_modules, target, ...). Only these are left out of full backups; a
    tracked or merely untracked folder that happens to be called 'target' is kept."""
    r = git.run(repo, "status", "--porcelain=v1", "-z", "--ignored=traditional", "--untracked-files=normal")
    dirs: set[str] = set()
    if r.ok:
        for item in r.out.split("\x00"):
            if item.startswith("!! ") and item.endswith("/"):
                rel = item[3:].rstrip("/")
                if rel.split("/")[-1] in CACHE_DIR_NAMES:
                    dirs.add(rel)
    return dirs


def _raise(err: OSError) -> None:
    raise err


def full_backup_files(git: Git, repo: Path) -> list[tuple[str, str]]:
    """(relative path, os path) of everything a full backup must hold: all of
    .git and the whole working tree, minus ignored cache folders. Raises OSError
    if any folder can't be read, so an incomplete backup is never mistaken for
    a complete one."""
    skip = ignored_cache_dirs(git, repo)
    root_s = longpath(repo)
    files: list[tuple[str, str]] = []
    for dirpath, dirnames, filenames in os.walk(root_s, onerror=_raise):
        rel_dir = os.path.relpath(dirpath, root_s).replace("\\", "/")
        rel_dir = "" if rel_dir == "." else rel_dir
        keep = []
        for d in sorted(dirnames):
            rel = f"{rel_dir}/{d}" if rel_dir else d
            if rel in skip or os.path.islink(os.path.join(dirpath, d)):
                continue
            keep.append(d)
        dirnames[:] = keep
        for fn in sorted(filenames):
            files.append((f"{rel_dir}/{fn}" if rel_dir else fn, os.path.join(dirpath, fn)))
    return files


_VOLATILE_GIT_FILES = {".git/index", ".git/FETCH_HEAD", ".git/gc.log"}


def fingerprint_full(git: Git, repo: Path) -> str:
    """Byte-level identity of a whole folder (the same file set a full backup
    holds). Only files git rewrites on its own (.git/index - its staged content is
    added via ls-files -s - FETCH_HEAD, locks) are left out. Used to avoid zipping
    the same unchanged folder twice and to confirm nothing changed before a delete."""
    h = hashlib.sha256(b"full-v2\n")
    for rel, full in full_backup_files(git, repo):
        if rel in _VOLATILE_GIT_FILES or (rel.startswith(".git/") and rel.endswith(".lock")):
            continue
        try:
            digest = ("link:" + os.readlink(full)) if os.path.islink(full) else sha256_file(full)
        except OSError as ex:
            raise OSError(f"can't read {rel}: {ex}") from None
        h.update(f"F {rel} {digest}\n".encode("utf-8", "replace"))
    staged = git.run(repo, "ls-files", "-s", "-z")
    h.update(b"S " + staged.out.encode("utf-8", "replace"))
    return h.hexdigest()


class BackupStore:
    """One folder per run that actually backed something up; manifest.jsonl at
    the root indexes every backup by (identity, kind, fingerprint) so the same
    state is never zipped twice."""

    def __init__(self, root: Optional[Path], stamp: str, dry_run: bool) -> None:
        self.root = root
        self.stamp = stamp
        self.dry_run = dry_run
        self.lock = threading.Lock()
        self.index: dict[tuple, str] = {}
        self.created: list[tuple[str, int]] = []
        self.reused = 0
        self.available = False
        self.error = ""
        if root is not None:
            self._open()

    def _open(self) -> None:
        assert self.root is not None
        mf = self.root / "manifest.jsonl"
        if self.dry_run:
            self.available = os.path.isdir(longpath(self.root)) or os.path.isdir(longpath(self.root.parent))
            if not self.available:
                self.error = f"backup folder {self.root} (and its parent) not found"
        else:
            try:
                os.makedirs(longpath(self.root), exist_ok=True)
                probe = self.root / f".write-test-{uuid.uuid4().hex[:8]}"
                with open(longpath(probe), "w", encoding="utf-8") as f:
                    f.write("ok")
                os.remove(longpath(probe))
                self.available = True
            except OSError as e:
                self.error = f"backup folder {self.root} is not writable ({e})"
                return
        if os.path.exists(longpath(mf)):
            with open(longpath(mf), encoding="utf-8", errors="replace") as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    k = (rec.get("identity"), rec.get("kind"), rec.get("fp"))
                    if all(k) and rec.get("file"):
                        self.index[k] = rec["file"]

    def have(self, ident_key: str, kind: str, fp: str) -> Optional[str]:
        with self.lock:
            f = self.index.get((ident_key, kind, fp))
        if f and self.root is not None and os.path.exists(longpath(self.root / f)):
            return f
        return None

    def _zip_target(self, ident_key: str, base: str, reason: str, fp: str) -> Path:
        assert self.root is not None
        name = f"{slug(ident_key.replace(':', '__').replace('/', '__'))}__{base}__{reason}__{fp[:8]}.zip"
        return self.root / self.stamp / name

    @staticmethod
    def _verify_and_commit(tmp: Path, final: Path, expected: int) -> tuple[bool, str]:
        try:
            with zipfile.ZipFile(longpath(tmp)) as zf:
                bad = zf.testzip()
                count = len(zf.infolist())
            if bad is not None:
                return False, f"zip check failed at {bad}"
            if count != expected:
                return False, f"zip has {count} entries, expected {expected}"
            os.replace(longpath(tmp), longpath(final))
            return True, ""
        except (OSError, zipfile.BadZipFile, RuntimeError) as e:
            return False, f"zip check failed ({e})"

    def _record(self, ident_key: str, kind: str, fp: str, final: Path, source: Path, reason: str) -> str:
        assert self.root is not None
        rel = final.relative_to(self.root).as_posix()
        size = os.path.getsize(longpath(final))
        rec = {"ts": utc_now().isoformat(timespec="seconds"), "identity": ident_key, "kind": kind,
               "fp": fp, "file": rel, "source": str(source), "reason": reason, "bytes": size,
               "tool": TOOL_VERSION}
        with self.lock:
            with open(longpath(self.root / "manifest.jsonl"), "a", encoding="utf-8") as f:
                f.write(json.dumps(rec) + "\n")
                f.flush()
                os.fsync(f.fileno())
            self.index[(ident_key, kind, fp)] = rel
            self.created.append((rel, size))
        return rel

    def backup_extras(self, git: Git, copy: Copy, ident_key: str, reason: str, entries: list[Entry],
                      extra_refs: list[tuple[str, str]], fp: str, cap: bool = False) -> tuple[bool, str, list[str]]:
        """Zip only what differs from GitHub: changed/untracked files plus a git
        bundle of every commit listed in extra_refs (branches, HEAD, stash@{n}, ...).
        cap=True is only for P52 work-in-progress snapshots: very big files are left
        out and the zip is recorded as *partial*, which never counts as the backup
        that allows a reset or delete. Returns (ok, file-or-error, skipped)."""
        if not self.available or self.root is None:
            return False, self.error or "no backup folder", []
        for kind in (("extras", "extras-partial") if cap else ("extras",)):
            existing = self.have(ident_key, kind, fp)
            if existing:
                with self.lock:
                    self.reused += 1
                return True, existing, []
        final = self._zip_target(ident_key, copy.base, reason, fp)
        tmp = final.with_name(final.name + ".partial")
        skipped: list[str] = []
        temp_refs: list[str] = []
        try:
            os.makedirs(longpath(final.parent), exist_ok=True)
            with tempfile.TemporaryDirectory(prefix="repo-sync-") as td:
                n = 0
                total = 0
                ref_map = []
                with zipfile.ZipFile(longpath(tmp), "w", zipfile.ZIP_DEFLATED, allowZip64=True,
                                     strict_timestamps=False) as zf:
                    if extra_refs:
                        # A bundle needs ref names, and HEAD / stash@{n} aren't under refs/:
                        # give every commit a short-lived name, bundle those, check the bundle.
                        for i, (name, sha) in enumerate(extra_refs):
                            tref = _backup_ref_name(name, i)
                            r = git.run(copy.path, "update-ref", tref, sha)
                            if not r.ok:
                                raise RepoError(f"couldn't mark {name} for the bundle: {r.why()}")
                            temp_refs.append(tref)
                            ref_map.append({"ref": name, "sha": sha, "in_bundle_as": tref})
                        bp = Path(td) / "extras.bundle"
                        r = git.run(copy.path, "bundle", "create", str(bp), *temp_refs, timeout=GIT_TIMEOUT_NET)
                        if not r.ok:
                            raise RepoError(f"git bundle failed: {r.why()}")
                        v = git.run(copy.path, "bundle", "list-heads", str(bp))
                        if not v.ok or any(t not in v.out for t in temp_refs):
                            raise RepoError(f"bundle check failed: {v.why()}")
                        zf.write(str(bp), "extras.bundle")
                        n += 1
                    for e in entries:
                        src = copy.path / e.path
                        lp = longpath(src)
                        if not os.path.isfile(lp):
                            continue
                        size = os.path.getsize(lp)
                        if cap and (size > WIP_MAX_FILE_BYTES or total + size > WIP_MAX_TOTAL_BYTES):
                            skipped.append(e.path)
                            continue
                        zf.write(lp, "worktree/" + e.path)
                        n += 1
                        total += size
                    meta = {"identity": ident_key, "source": str(copy.path), "place": copy.base,
                            "reason": reason, "created": utc_now().isoformat(timespec="seconds"),
                            "commits": ref_map,
                            "changed_files": [f"{e.xy} {e.path}" for e in entries],
                            "too_big_not_zipped": skipped,
                            "restore": "worktree/ = the changed files as they were. Commits: in any clone run "
                                       "git fetch <path>/extras.bundle \"refs/*:refs/restored/*\" then git log --all"}
                    zf.writestr("meta.json", json.dumps(meta, indent=2))
                    n += 1
            ok, msg = self._verify_and_commit(tmp, final, n)
            if not ok:
                raise RepoError(msg)
            rel = self._record(ident_key, "extras-partial" if skipped else "extras", fp, final, copy.path, reason)
            return True, rel, skipped
        except (OSError, RepoError, zipfile.BadZipFile) as e:
            try:
                os.remove(longpath(tmp))
            except OSError:
                pass
            return False, f"backup failed: {e}", skipped
        finally:
            for t in temp_refs:
                git.run(copy.path, "update-ref", "-d", t)

    def backup_full(self, git: Git, copy: Copy, ident_key: str, reason: str, fp: str) -> tuple[bool, str]:
        """Complete folder zip: all of .git + the whole working tree, minus only
        cache folders git itself ignores. Must succeed and verify before anything
        is deleted."""
        if not self.available or self.root is None:
            return False, self.error or "no backup folder"
        existing = self.have(ident_key, "full", fp)
        if existing:
            with self.lock:
                self.reused += 1
            return True, existing
        final = self._zip_target(ident_key, copy.base, reason, fp)
        tmp = final.with_name(final.name + ".partial")
        try:
            files = full_backup_files(git, copy.path)
            os.makedirs(longpath(final.parent), exist_ok=True)
            n = 0
            with zipfile.ZipFile(longpath(tmp), "w", zipfile.ZIP_DEFLATED, allowZip64=True,
                                 strict_timestamps=False) as zf:
                for rel, full in files:
                    zf.write(full, f"{copy.folder}/{rel}")
                    n += 1
                meta = {"identity": ident_key, "source": str(copy.path), "place": copy.base,
                        "reason": reason, "created": utc_now().isoformat(timespec="seconds"),
                        "left_out": "only cache folders git ignores: " + ", ".join(sorted(CACHE_DIR_NAMES)),
                        "restore": f"unzip; the folder {copy.folder}/ is the repo exactly as it was"}
                zf.writestr("meta.json", json.dumps(meta, indent=2))
                n += 1
            ok, msg = self._verify_and_commit(tmp, final, n)
            if not ok:
                raise RepoError(msg)
            return True, self._record(ident_key, "full", fp, final, copy.path, reason)
        except (OSError, RepoError, zipfile.BadZipFile, ValueError) as e:
            try:
                os.remove(longpath(tmp))
            except OSError:
                pass
            return False, f"backup failed: {e}"


def _backup_ref_name(name: str, i: int) -> str:
    """refs/repo-sync/backup/000/heads/feat, .../001/HEAD, .../002/stash-1 ..."""
    if name.startswith("refs/"):
        tail = name[len("refs/"):]
    else:
        tail = re.sub(r"[^A-Za-z0-9._/-]+", "-", name).strip("-/.") or "ref"
    return f"refs/repo-sync/backup/{i:03d}/{tail}"


# ---------------------------------------------------------------------------
# CONTEXT, SCANNING, CLASSIFICATION
# ---------------------------------------------------------------------------


@dataclass
class Ctx:
    git: Git
    api: GitHubApi
    args: argparse.Namespace
    me: str
    owners: list                 # priority order, you first
    owned: set                   # lower-case owner names
    unlisted: set                # owners whose repo listing failed (lower-case)
    online: dict                 # "owner/name" (lower) -> OnlineRepo
    store: BackupStore
    templates: dict
    p52_base: Path
    nas_base: Optional[Path]
    names_present: dict          # base -> set of lower-case names already in that folder
    stop: threading.Event = field(default_factory=threading.Event)
    deletes_allowed: bool = True

    def owner_rank(self, owner: str) -> int:
        low = [o.lower() for o in self.owners]
        return low.index(owner.lower()) if owner.lower() in low else len(low)

    def base_path(self, base: str) -> Optional[Path]:
        return self.p52_base if base == P52 else self.nas_base


def read_origin(git: Git, repo: Path) -> Optional[str]:
    r = git.run(repo, "config", "--get", "remote.origin.url")
    val = r.out.strip()
    return val if r.ok and val else None


def scan_base(git: Git, label: str, base: Path, jobs: int) -> tuple[list[Copy], set, list[str]]:
    try:
        entries = sorted(os.scandir(longpath(base)), key=lambda e: e.name.lower())
    except OSError as e:
        raise SystemExit(f"Cannot read the {label} folder {base}: {e}") from None
    names = {e.name.lower() for e in entries}
    notes: list[str] = []
    repos: list[Path] = []
    for e in entries:
        try:
            if not e.is_dir():
                continue
        except OSError:
            continue
        if e.name in SKIP_DIR_NAMES:
            continue
        if TOMBSTONE_SUFFIX in e.name:
            notes.append(f"leftover from an interrupted delete (safe to remove by hand): {base / e.name}")
            continue
        p = base / e.name
        if os.path.isdir(longpath(p / ".git")):
            repos.append(p)
        elif os.path.isfile(longpath(p / ".git")):
            notes.append(f"{label}:{e.name} is a linked worktree/submodule checkout - skipped")

    def mk(p: Path) -> Copy:
        url = read_origin(git, p)
        return Copy(label, p, p.name, url, parse_remote_url(url))

    with ThreadPoolExecutor(max_workers=max(1, jobs)) as ex:
        copies = list(ex.map(mk, repos))
    return copies, names, notes


def looks_not_found(err: str) -> bool:
    """git's wording when the repository itself doesn't exist (vs network/auth trouble)."""
    e = err.lower()
    if "could not resolve" in e or "timed out" in e or "authentication failed" in e:
        return False
    return ("repository not found" in e
            or ("repository '" in e and "' not found" in e)
            or "does not appear to be a git repository" in e
            or "returned error: 404" in e)


def ls_remote_status(git: Git, url: str, cwd: Optional[Path] = None) -> tuple[str, str]:
    """('exists'|'not-found'|'unsure', detail) using your normal git credentials."""
    r = git.run(cwd, "ls-remote", url, timeout=GIT_TIMEOUT_NET)
    if r.ok:
        return "exists", ""
    return ("not-found" if looks_not_found(r.err) else "unsure"), r.why()


def commits_present(git: Git, repo: Path, shas: Iterable[str]) -> bool:
    data = "".join(f"{s}\n" for s in shas)
    if not data:
        return False
    r = git.run(repo, "cat-file", "--batch-check", input_text=data)
    return any(line.strip() and not line.strip().endswith("missing") for line in r.out.splitlines())


def copy_roots(ctx: Ctx, c: Copy) -> frozenset:
    if c.roots is None:
        c.roots = root_commits(ctx.git, c.path)
    return c.roots


def same_lineage(ctx: Ctx, a: Copy, b: Copy) -> bool:
    ra, rb = copy_roots(ctx, a), copy_roots(ctx, b)
    if not ra or not rb:
        return not ra and not rb   # two empty repos match; empty vs real history never does
    return bool(ra & rb)


def probe_online_lineage(ctx: Ctx, c: Copy, cands: list[OnlineRepo]) -> tuple[Optional[OnlineRepo], str]:
    """A folder with no origin: is it one of your online repos? Decided by
    shared history (never by name alone)."""
    roots = copy_roots(ctx, c)
    if not roots:
        return None, ""
    _, head = head_state(ctx.git, c.path)
    related: list[tuple[OnlineRepo, bool]] = []
    for r in sorted(cands, key=lambda x: (ctx.owner_rank(x.owner), x.key)):
        lr = ctx.git.run(c.path, "ls-remote", r.url, timeout=GIT_TIMEOUT_NET)
        if not lr.ok:
            continue
        shas = {ln.split()[0] for ln in lr.out.splitlines() if ln.strip()}
        if not shas:
            continue
        exact = bool(head and head in shas)
        if commits_present(ctx.git, c.path, shas):
            related.append((r, exact))
            continue
        probe = f"refs/repo-sync/probe/{uuid.uuid4().hex[:12]}"
        branch = r.default_branch or "main"
        try:
            fr = ctx.git.run(c.path, "fetch", "--no-tags", "--quiet", r.url, f"+refs/heads/{branch}:{probe}",
                             timeout=GIT_TIMEOUT_NET)
            if fr.ok:
                pr = ctx.git.run(c.path, "rev-list", "--max-parents=0", probe)
                if set(pr.out.split()) & roots:
                    related.append((r, exact))
        finally:
            ctx.git.run(c.path, "update-ref", "-d", probe)
    if not related:
        return None, ""
    exact = [r for r, ex in related if ex]
    pick = exact[0] if len(exact) == 1 else related[0][0]
    note = ""
    if len(related) > 1:
        note = f"{c.label} shares history with {', '.join(r.full for r, _ in related)}; matched {pick.full}"
    return pick, note


def classify(ctx: Ctx, copies: list[Copy], jobs: int) -> dict[str, Identity]:
    idents: dict[str, Identity] = {}

    def online_ident(repo: OnlineRepo) -> Identity:
        key = "gh:" + repo.key
        if key not in idents:
            idents[key] = Identity(key, ONLINE, online=repo, url=repo.url)
        return idents[key]

    def skip_ident(key: str, c: Copy, kind: str, msg: str, hint: str = "") -> None:
        ident = idents.setdefault(key, Identity(key, SKIPPED))
        ident.copies[c.base].append(c)
        if not any(i.message == msg for i in ident.issues):
            ident.issues.append(Issue(key, None, kind, msg, hint))

    unresolved: dict[str, list[Copy]] = {}
    no_origin: list[Copy] = []
    for c in copies:
        rem = c.remote
        if rem is None:
            no_origin.append(c)
        elif rem.kind == "github":
            if rem.key in ctx.online:
                online_ident(ctx.online[rem.key]).copies[c.base].append(c)
            else:
                unresolved.setdefault(rem.key, []).append(c)
        elif rem.kind == "other":
            key = "foreign:" + rem.norm()
            ident = idents.setdefault(key, Identity(key, FOREIGN, url=c.origin_url))
            ident.copies[c.base].append(c)
        else:
            skip_ident(f"skip:{c.base}:{c.folder.lower()}", c, "local-origin",
                       f"{c.label}: origin is a local path ({c.origin_url}) - left alone",
                       "point origin at GitHub if it belongs there")

    # Origins that aren't in the listing: moved/renamed (GitHub redirects), deleted,
    # never created, or someone else's repo.
    def resolve(key: str) -> tuple[str, int, Optional[OnlineRepo], str]:
        owner, name = key.split("/", 1)
        c0 = unresolved[key][0]
        try:
            status, repo, msg = ctx.api.get_repo(c0.remote.owner, c0.remote.name)  # type: ignore[union-attr]
        except ApiError as e:
            return key, e.status, None, e.message
        return key, status, repo, msg

    with ThreadPoolExecutor(max_workers=max(1, jobs)) as ex:
        resolutions = list(ex.map(resolve, sorted(unresolved)))

    def settle_404(key: str) -> tuple[str, str, Optional[OnlineRepo], str]:
        """Origin names an account of yours but GitHub says 404. Before believing
        'deleted': is it one of your repos under a slightly different name (the old
        script rewrote origins to kebab-case names) or in another of your accounts?
        Only shared history counts as proof."""
        cs = unresolved[key]
        owner, name = cs[0].remote.owner, cs[0].remote.name  # type: ignore[union-attr]
        k = to_kebab(name)
        same_owner = [r for r in ctx.online.values() if r.owner.lower() == owner.lower() and to_kebab(r.name) == k]
        cands = same_owner or [r for r in ctx.online.values() if to_kebab(r.name) == k]
        if cands:
            for c in cs:
                pick, note = probe_online_lineage(ctx, c, cands)
                if pick is not None:
                    return key, "moved", pick, note
        state, why = ls_remote_status(ctx.git, cs[0].origin_url or "", None)
        if state == "not-found":
            evidence = any(has_any_ref(ctx.git, c.path, "refs/remotes/origin") for c in cs)
            return key, ("gone" if evidence else "never"), None, why
        return key, state, None, why

    owned_404 = [k for k, st, _, _ in resolutions
                 if st == 404 and k.split("/", 1)[0] in ctx.owned and k.split("/", 1)[0] not in ctx.unlisted]
    with ThreadPoolExecutor(max_workers=max(1, jobs)) as ex:
        settled = {k: (st, pick, why) for k, st, pick, why in ex.map(settle_404, owned_404)}

    for key, status, repo, msg in resolutions:
        cs = unresolved[key]
        owner, name = cs[0].remote.owner, cs[0].remote.name  # type: ignore[union-attr]
        if status == 200 and repo is not None:
            if repo.owner.lower() in ctx.owned:
                repo = ctx.online.setdefault(repo.key, repo)
                ident = online_ident(repo)
                if repo.key != key:
                    ident.moved_from.add(f"{owner}/{name}")
                for c in cs:
                    ident.copies[c.base].append(c)
            else:
                fkey = "foreign:github.com/" + repo.key
                ident = idents.setdefault(fkey, Identity(fkey, FOREIGN, url=cs[0].origin_url, online=repo))
                for c in cs:
                    ident.copies[c.base].append(c)
        elif key in settled:
            state, pick, why = settled[key]
            if state == "moved" and pick is not None:
                ident = online_ident(pick)
                ident.moved_from.add(f"{owner}/{name}")
                ident.notes.append(why or f"origin said {owner}/{name} (not on GitHub); matched {pick.full} by history")
                for c in cs:
                    ident.copies[c.base].append(c)
            elif state == "exists":
                for c in cs:
                    skip_ident("skip:" + key, c, "token-blind",
                               f"{owner}/{name}: your token can't see it but git can - not touched",
                               "give the token access to that org (SSO authorise / read:org)")
            elif state == "gone":
                gkey = "gone:" + key
                ident = idents.setdefault(gkey, Identity(gkey, GONE, url=cs[0].origin_url))
                ident.online = OnlineRepo(owner=owner, name=name)
                for c in cs:
                    ident.copies[c.base].append(c)
            elif state == "never":
                lkey = "local:" + key
                ident = idents.setdefault(lkey, Identity(lkey, LOCAL_ONLY))
                ident.new_repo = (owner, name)
                ident.notes.append(f"origin points at {owner}/{name}, which was never created")
                for c in cs:
                    ident.copies[c.base].append(c)
            else:
                for c in cs:
                    skip_ident("skip:" + key, c, "unsure",
                               f"{owner}/{name}: not online per the API, but git couldn't confirm ({why}) - not touched")
        elif status == 404:
            if owner.lower() in ctx.unlisted:
                for c in cs:
                    skip_ident("skip:" + key, c, "unlisted-owner",
                               f"{owner}/{name}: couldn't list {owner}'s repos this run - not touched")
            else:
                for c in cs:
                    skip_ident("skip:" + key, c, "foreign-gone",
                               f"{owner}/{name}: someone else's repo that no longer exists online - left alone")
        else:
            for c in cs:
                skip_ident("skip:" + key, c, "api",
                           f"{owner}/{name}: GitHub API said {status} {msg} - not touched this run")

    # Folders with no origin: match a twin folder on the other side by history,
    # then your online repos by history, otherwise it's never been online.
    by_folder: dict[tuple, tuple[Identity, Copy]] = {}
    for ident in idents.values():
        for c in ident.all_copies():
            by_folder[(c.base, c.folder.lower())] = (ident, c)

    def attach(ident: Identity, c: Copy) -> None:
        ident.copies[c.base].append(c)
        by_folder[(c.base, c.folder.lower())] = (ident, c)

    for base in BASES:
        other = NAS if base == P52 else P52
        pending: list[Copy] = []
        for c in [x for x in no_origin if x.base == base]:
            twin = by_folder.get((other, c.folder.lower()))
            if twin and same_lineage(ctx, c, twin[1]):   # includes SKIPPED twins: never publish around a skip
                attach(twin[0], c)
                if twin[0].kind != LOCAL_ONLY:
                    twin[0].notes.append(f"{c.label} had no origin; matched by history to {twin[1].label}")
            else:
                pending.append(c)

        def probe(c: Copy) -> tuple[Copy, Optional[OnlineRepo], str]:
            k = to_kebab(c.folder)
            cands = [r for r in ctx.online.values() if to_kebab(r.name) == k]
            if not cands:
                return c, None, ""
            pick, note = probe_online_lineage(ctx, c, cands)
            return c, pick, note

        with ThreadPoolExecutor(max_workers=max(1, jobs)) as ex:
            probed = list(ex.map(probe, pending))
        for c, pick, note in probed:
            if pick is not None:
                ident = online_ident(pick)
                attach(ident, c)
                ident.notes.append(note or f"{c.label} had no origin; matched {pick.full} by history")
                continue
            key = "local:" + c.folder.lower()
            ident = idents.get(key)
            if ident is not None and any(not same_lineage(ctx, c, x) for x in ident.all_copies()):
                key = f"local:{c.folder.lower()}#{base.lower()}"
                ident = idents.get(key)
            if ident is None:
                ident = Identity(key, LOCAL_ONLY)
                idents[key] = ident
            attach(ident, c)

    for repo in ctx.online.values():
        online_ident(repo)
    return idents


def default_online_folder(ctx: Ctx, repo: OnlineRepo, by_name: dict) -> str:
    same = by_name.get(repo.name.lower(), [repo])
    if len(same) <= 1:
        return safe_folder_name(repo.name)
    top = min(same, key=lambda r: (ctx.owner_rank(r.owner), r.owner.lower()))
    return safe_folder_name(repo.name) if top.key == repo.key else safe_folder_name(f"{repo.name}--{repo.owner}")


KIND_ORDER = {ONLINE: 0, FOREIGN: 1, LOCAL_ONLY: 2, GONE: 3, SKIPPED: 4}


def ident_sort_key(ctx: Ctx, ident: Identity) -> tuple:
    owner = ident.online.owner if ident.online else ""
    return (KIND_ORDER.get(ident.kind, 9), ctx.owner_rank(owner) if owner else 99, ident.key)


def plan_targets(ctx: Ctx, idents: dict[str, Identity]) -> None:
    """Pick the one real copy per place, and the folder name for anything that
    needs cloning. Existing folders keep their names; the NAS follows the P52."""
    taken = {b: set(ctx.names_present.get(b, set())) for b in BASES}
    by_name: dict[str, list[OnlineRepo]] = {}
    for r in ctx.online.values():
        by_name.setdefault(r.name.lower(), []).append(r)

    for ident in idents.values():
        for base in BASES:
            cs = ident.copies[base]
            if len(cs) == 1:
                ident.canonical[base] = cs[0]
            elif len(cs) > 1:
                other = ident.copies[NAS if base == P52 else P52]
                prefs: list[str] = []
                if len(other) == 1:
                    prefs.append(other[0].folder)
                if ident.online is not None:
                    prefs += [ident.online.name, safe_folder_name(ident.online.name),
                              default_online_folder(ctx, ident.online, by_name)]
                pick = None
                for pref in prefs:
                    pick = next((c for c in cs if c.folder.lower() == pref.lower()), None)
                    if pick:
                        break
                ident.canonical[base] = pick
                names = ", ".join(c.folder for c in cs)
                if pick:
                    msg = f"{len(cs)} folders on the {base} are the same repo ({names}); syncing only '{pick.folder}'"
                else:
                    msg = f"{len(cs)} folders on the {base} are the same repo ({names}); can't tell which is real, none synced there"
                ident.issues.append(Issue(ident.key, base, "duplicate", msg,
                                          "keep one; 'repo_sync.py dupes' can clear identical ones"))

    reserved_new: set[str] = {f"{i.new_repo[0].lower()}/{i.new_repo[1].lower()}"
                              for i in idents.values() if i.kind == LOCAL_ONLY and i.new_repo}
    # what already sits in each folder name (a third-party GitHub repo is "foreign";
    # an origin we couldn't read at all is "foreign-unknown")
    occupant: dict[tuple, str] = {}
    for i in idents.values():
        mark = "foreign-unknown" if (i.kind == FOREIGN and i.online is None) else i.kind
        for c in i.all_copies():
            occupant[(c.base, c.folder.lower())] = mark
    for ident in sorted(idents.values(), key=lambda i: ident_sort_key(ctx, i)):
        if ident.kind not in (ONLINE, FOREIGN, LOCAL_ONLY):
            continue
        if ident.kind == ONLINE and ident.online and ident.online.disabled:
            ident.issues.append(Issue(ident.key, None, "disabled", f"{ident.online.full} is disabled on GitHub - skipped"))
            continue
        p, n = ident.canonical[P52], ident.canonical[NAS]
        if p is not None:
            name = p.folder
        elif n is not None:
            name = n.folder
        elif ident.kind == ONLINE and ident.online:
            name = default_online_folder(ctx, ident.online, by_name)
        elif ident.kind == FOREIGN:
            rem = parse_remote_url(ident.url)
            name = safe_folder_name(rem.name if rem and rem.name else "repo")
        else:
            continue
        bases = [b for b in BASES if not (b == NAS and ctx.nas_base is None)]
        for base in bases:
            if ident.canonical[base] is not None:
                ident.target[base] = ident.canonical[base].folder
        # places that need a fresh clone (duplicates without a clear winner get nothing added)
        need = [b for b in bases if ident.canonical[b] is None and not ident.copies[b]]
        for base in list(need):
            if occupant.get((base, name.lower())) in (SKIPPED, "foreign-unknown"):
                # that folder holds a repo whose origin we couldn't recognise - it may BE this one
                need.remove(base)
                ident.issues.append(Issue(ident.key, base, "maybe-same",
                                          f"not cloning a second copy on the {base}: folder '{name}' there holds a repo "
                                          f"whose origin isn't recognised as GitHub - it may be this one",
                                          "fix that folder's origin (git remote set-url origin <url>), then run again"))
        if need:
            owner = ident.online.owner if ident.online else ""
            options = [name]
            if owner and not name.lower().endswith(f"--{owner.lower()}"):
                options.append(safe_folder_name(f"{name}--{owner}"))
            # one name that's free everywhere it's needed, so the P52 and NAS agree
            both = next((o for o in options if all(o.lower() not in taken[b] for b in need)), None)
            for base in need:
                chosen = both or next((o for o in options if o.lower() not in taken[base]), None)
                if chosen is None:
                    ident.issues.append(Issue(ident.key, base, "name-taken",
                                              f"can't create it on the {base}: folder '{name}' is already used by something else",
                                              "rename or move that folder"))
                else:
                    ident.target[base] = chosen
                    taken[base].add(chosen.lower())
        if (p is not None and n is not None and n.folder.lower() != p.folder.lower()
                and not ctx.args.no_rename_nas and ctx.nas_base is not None):
            if p.folder.lower() in taken[NAS]:
                ident.notes.append(f"NAS folder is '{n.folder}', P52 is '{p.folder}' (name taken on NAS, left as is)")
            else:
                ident.target[NAS] = p.folder
                taken[NAS].add(p.folder.lower())

        if ident.kind == LOCAL_ONLY and ident.new_repo is None:
            owner = ctx.args.new_repo_owner or ctx.me
            src = p or n
            if src is None:
                continue
            rname = github_repo_name(src.folder)
            k = f"{owner.lower()}/{rname.lower()}"
            if k in ctx.online or k in reserved_new:
                ident.issues.append(Issue(ident.key, None, "name-clash",
                                          f"'{src.folder}' has never been online, but {owner}/{rname} already exists "
                                          f"online with different history - not published",
                                          "rename the folder (a new repo gets its name), or set its origin by hand"))
            else:
                ident.new_repo = (owner, rname)
                reserved_new.add(k)


# ---------------------------------------------------------------------------
# PER-REPO SYNC (the same code produces the plan and applies it)
# ---------------------------------------------------------------------------


def same_remote(a: Optional[str], b: Optional[str]) -> bool:
    if not a or not b:
        return False
    ra, rb = parse_remote_url(a), parse_remote_url(b)
    if ra and rb and ra.kind == "github" and rb.kind == "github":
        return ra.key == rb.key
    return a.strip().rstrip("/").lower() == b.strip().rstrip("/").lower()


def ff_would_clobber(git: Git, repo: Path, up: str, entries: list[Entry]) -> list[str]:
    """Files a fast-forward would need to touch that you've changed locally."""
    if not entries:
        return []
    r = git.run(repo, "diff", "--name-only", "-z", "HEAD", up)
    changed = {x for x in r.out.split("\x00") if x}
    return sorted(changed & {e.path for e in entries})


class Runner:
    def __init__(self, ctx: Ctx, ident: Identity, apply: bool, planned: Optional[set]) -> None:
        self.ctx = ctx
        self.git = ctx.git
        self.ident = ident
        self.apply = apply
        self.planned = planned
        self.R = Result(ident.key, ident.display, ident.kind)

    # -- helpers ------------------------------------------------------------

    def gate(self, s: Step) -> bool:
        """Apply mode only; changes you'd care about must match the approved plan."""
        if not self.apply:
            return False
        if self.ctx.stop.is_set():
            s.status, s.error = "skipped", "stopped by you"
            return False
        if s.kind in GATED and self.planned is not None and s.sig not in self.planned:
            s.status = "skipped"
            s.error = "wasn't in the plan you approved (something changed) - run again to review"
            self.R.issue(s.base, "changed", f"{s.detail}: skipped, {s.error}")
            return False
        return True

    def done(self, s: Step, ok: bool, err: str = "") -> bool:
        s.status = "done" if ok else "failed"
        s.error = "" if ok else (err or "failed")
        if not ok:
            self.R.issue(s.base, "failed", f"{s.detail}: FAILED - {s.error}")
        return ok

    def wanted_origin(self, c: Copy, online: Optional[OnlineRepo]) -> Optional[str]:
        if online is None:
            return None
        if c.remote is not None and c.remote.kind == "github":
            if c.remote.key == online.key:
                return c.origin_url
            return c.remote.with_repo(online.owner, online.name)
        return online.url

    def fix_origin(self, c: Copy, online: Optional[OnlineRepo]) -> Optional[str]:
        want = self.wanted_origin(c, online)
        if want is None or same_remote(c.origin_url, want):
            return want
        s = self.R.step(c.base, "retarget", online.full if online else "",
                        f"{c.label}: origin -> {redact_url(want)} (was {redact_url(c.origin_url) or 'not set'})")
        if self.gate(s):
            if c.origin_url:
                r = self.git.run(c.path, "remote", "set-url", "origin", want)
            else:
                r = self.git.run(c.path, "remote", "add", "origin", want)
            if self.done(s, r.ok, r.why()):
                c.origin_url, c.remote = want, parse_remote_url(want)
        return want

    def remote_spec(self, c: Copy, want: Optional[str]) -> str:
        """'origin', or - in the plan pass, before a wrong origin URL is fixed - the right URL."""
        if want and not same_remote(read_origin(self.git, c.path), want):
            return want
        return "origin"

    def fetch(self, c: Copy, spec: str) -> GitResult:
        # Never prune while fetching: a remote-tracking ref whose branch has vanished from
        # GitHub is the evidence that it was deleted online (rather than never pushed).
        cfg = ("-c", "fetch.prune=false", "-c", "remote.origin.prune=false")
        if spec == "origin":
            return self.git.run(c.path, *cfg, "fetch", "--quiet", "origin", timeout=GIT_TIMEOUT_NET)
        return self.git.run(c.path, *cfg, "fetch", "--quiet", spec, "+refs/heads/*:refs/remotes/origin/*",
                            timeout=GIT_TIMEOUT_NET)

    def remote_state(self, c: Copy, online: Optional[OnlineRepo],
                     source: Optional[Path] = None) -> Optional["RemoteState"]:
        """Fix origin if needed, see which branches GitHub has right now, and fetch
        only if something there is new to this copy (one network call when nothing
        changed). source: plan pass for a repo that's about to be published - look
        at that local repo instead, it's exactly what GitHub will hold."""
        ns = TRACKING_NS
        if source is not None:
            spec, ns = str(source), PREVIEW_NS
        else:
            want = self.fix_origin(c, online)
            spec = self.remote_spec(c, want)
        refs = ls_remote_refs(self.git, c.path, spec)
        if refs is None:
            self.R.issue(c.base, "fetch", f"couldn't reach {self.ident.display} on GitHub from {c.label}",
                         f'check: cd "{c.path}"; git fetch origin')
            return None
        live = {k[len("refs/heads/"):]: v for k, v in refs.items() if k.startswith("refs/heads/")}
        if ns == PREVIEW_NS:
            f = self.git.run(c.path, "fetch", "--quiet", "--no-tags", "--prune", spec,
                             f"+refs/heads/*:{PREVIEW_NS}/*", timeout=GIT_TIMEOUT_NET)
            if not f.ok:
                self.R.issue(c.base, "fetch", f"couldn't preview {c.label} against {spec}: {f.why()}")
                return None
            return RemoteState(spec, live, origin_refs(self.git, c.path, ns), ns)
        tracking = origin_refs(self.git, c.path)
        need = any(tracking.get(b) != sha for b, sha in live.items())
        if not need:
            tags = [k for k in refs if k.startswith("refs/tags/") and not k.endswith("^{}")]
            if tags:
                have = set(self.git.run(c.path, "for-each-ref", "--format=%(refname)", "refs/tags").out.split())
                need = any(t not in have for t in tags)
        if need:
            f = self.fetch(c, spec)
            if not f.ok:
                self.R.issue(c.base, "fetch", f"couldn't fetch {self.ident.display} into {c.label}: {f.why()}",
                             f'check: cd "{c.path}"; git fetch origin')
                return None
            tracking = origin_refs(self.git, c.path)
        return RemoteState(spec, live, tracking)

    def fix_head_ref(self, c: Copy, default: Optional[str]) -> None:
        if not self.apply or not default:
            return
        want = f"refs/remotes/origin/{default}"
        cur = self.git.run(c.path, "symbolic-ref", "-q", "refs/remotes/origin/HEAD").out.strip()
        if cur != want and ref_sha(self.git, c.path, want) and read_origin(self.git, c.path):
            self.git.run(c.path, "remote", "set-head", "origin", default)

    @staticmethod
    def upstream_of(b: Branch, rs: "RemoteState") -> tuple[str, Optional[str]]:
        """('ok' | 'gone-seen' | 'gone-unseen' | 'none' | 'other', its branch name on GitHub).
        gone-seen   = its GitHub branch was deleted (we still hold the old tracking ref)
        gone-unseen = tracks a GitHub branch that doesn't exist and never showed up here
        Compare against rs.ref(name)."""
        remotes, stale = rs.remotes, rs.stale
        if b.upstream:
            if b.upstream_remote != "origin" or not b.upstream.startswith(TRACKING_NS + "/"):
                return "other", None
            short = b.upstream[len(TRACKING_NS) + 1:]
            if short in remotes:
                return "ok", short
            return ("gone-seen" if short in stale else "gone-unseen"), short
        if b.name in remotes:
            return "ok", b.name
        if b.name in stale:
            return "gone-seen", b.name
        return "none", None

    def unrelated_history(self, c: Copy, rs: "RemoteState") -> bool:
        """True when this copy and the GitHub repo share no history at all - e.g. an old
        repo name reused for a brand-new repo. Then nothing may be pushed, reset or deleted."""
        if not rs.live:
            return False
        git, repo = self.git, c.path
        local = [f"refs/heads/{b.name}" for b in list_branches(git, repo)]
        if not local:
            return False
        if count_not_in(git, repo, local, rs.live_refs) == 0:
            return False                    # everything here is already in GitHub's history
        mine = git.run(repo, "rev-list", "--max-parents=0", "--stdin", input_text="\n".join(local) + "\n")
        theirs = git.run(repo, "rev-list", "--max-parents=0", "--stdin", input_text="\n".join(rs.live_refs) + "\n")
        if not mine.ok or not theirs.ok:
            return True                     # can't prove they're related: hands off
        return not (set(mine.out.split()) & set(theirs.out.split()))

    def prune_stale(self, c: Copy, rs: "RemoteState", exclude: set) -> None:
        """Forget remote-tracking refs of branches deleted on GitHub; back up any
        whose commits exist nowhere else in this copy first."""
        stale = {k: v for k, v in rs.stale.items() if k not in exclude}
        if not stale:
            return
        git, repo = self.git, c.path
        keep = rs.live_refs + [f"refs/heads/{b.name}" for b in list_branches(git, repo)]
        uniq = [(rs.ref(k), v) for k, v in sorted(stale.items()) if count_not_in(git, repo, rs.ref(k), keep) != 0]
        s: Optional[Step] = None
        if uniq:
            if not self.backup_extras_step(c, [], uniq, "deleted-online",
                                           f"{len(uniq)} branch(es) deleted on GitHub (their commits aren't anywhere else here)"):
                return
            s = self.R.step(c.base, "prune-refs", "origin:" + short_digest(uniq),
                            f"{c.label}: forget {len(stale)} branch(es) deleted on GitHub (backed up)")
            if not self.gate(s):
                return
        elif not self.apply:
            return
        err = ""
        for k, sha in stale.items():
            r = git.run(repo, "update-ref", "-d", rs.ref(k), sha)
            if not r.ok:
                err = r.why()
        if s is not None:
            self.done(s, not err, err)
        elif err:
            self.R.info(f"{c.label}: couldn't forget some deleted GitHub branches: {err}")

    def backup_extras_step(self, c: Copy, entries: list[Entry], refs: list[tuple[str, str]],
                           reason: str, what: str, wip: bool = False) -> bool:
        """Back up what exists only in this copy. True when it's safe to go on and
        change or delete it. wip=True is a P52 safety snapshot (nothing gets
        destroyed): there, very big files may be left out."""
        fp = fingerprint_extras(c.path, entries, refs)
        for kind in (("extras", "extras-partial") if wip else ("extras",)):
            existing = self.ctx.store.have(self.ident.key, kind, fp)
            if existing:
                self.R.info(f"{c.label}: {what} already backed up ({existing})")
                return True
        s = self.R.step(c.base, "backup", reason, f"{c.label}: back up {what}")
        if not self.apply:
            return True
        ok, res, skipped = self.ctx.store.backup_extras(self.git, c, self.ident.key, reason, entries, refs, fp,
                                                        cap=wip)
        self.done(s, ok, res)
        if ok:
            self.R.backups.append(res)
        if skipped:
            self.R.info(f"{c.label}: too big for the snapshot, left in place: {', '.join(skipped[:5])}")
        return ok and not skipped

    def gate_all(self, steps: list) -> bool:
        """All-or-nothing: every step of one change must be in the approved plan."""
        ok = all([self.gate(st) for st in steps])
        if not ok:
            for st in steps:
                if st.status == "planned":
                    st.status, st.error = "skipped", "held back: part of this change wasn't in the plan"
        return ok

    def clone(self, base: str) -> None:
        folder = self.ident.target[base]
        root = self.ctx.base_path(base)
        url = self.ident.url
        if not folder or root is None or not url:
            return
        dest = root / folder
        s = self.R.step(base, "clone", folder, f"clone {self.ident.display} to the {base} as '{folder}'")
        if not self.gate(s):
            return
        if os.path.lexists(longpath(dest)):
            self.done(s, False, f"{dest} appeared since the plan")
            return
        args = ["clone", "--origin", "origin"]
        if IS_WINDOWS:
            args += ["--config", "core.longpaths=true"]
        r = self.git.run(None, *args, url, str(dest), timeout=GIT_TIMEOUT_CLONE)
        self.done(s, r.ok, r.why())

    # -- entry point --------------------------------------------------------

    def process(self) -> Result:
        for iss in self.ident.issues:
            self.R.issues.append(iss)
        for note in self.ident.notes:
            self.R.info(note)
        if self.ctx.stop.is_set():
            return self.R
        try:
            if self.ident.kind == SKIPPED:
                pass
            elif self.ident.kind == GONE:
                self.delete_all_copies("deleted-online", "deleted on GitHub")
            elif self.ident.kind == LOCAL_ONLY:
                self.do_local_only()
            else:
                self.do_synced(self.ident.online if self.ident.kind == ONLINE else None)
        except RepoError as e:
            self.R.issue(None, "error", str(e))
        except Exception as e:  # keep going with the other repos, but say exactly what broke
            self.R.issue(None, "crash", f"unexpected error: {e!r}")
            self.R.info(traceback.format_exc())
        return self.R

    def do_synced(self, online: Optional[OnlineRepo]) -> None:
        p = self.ident.canonical[P52]
        if p is None:
            if not self.ident.copies[P52]:
                self.clone(P52)
        else:
            self.sync_work_copy(p, online)
        if self.ctx.nas_base is None:
            return
        n = self.ident.canonical[NAS]
        if n is None:
            if not self.ident.copies[NAS]:
                self.clone(NAS)
            return
        n = self.maybe_rename_nas(n)
        self.sync_mirror(n, online)

    def maybe_rename_nas(self, n: Copy) -> Copy:
        want = self.ident.target[NAS]
        if not want or want.lower() == n.folder.lower() or self.ctx.nas_base is None:
            return n
        dst = self.ctx.nas_base / want
        s = self.R.step(NAS, "rename-folder", want, f"NAS: rename '{n.folder}' -> '{want}' to match the P52")
        if not self.gate(s):
            return n
        if os.path.lexists(longpath(dst)):
            self.done(s, False, f"{dst} already exists")
            return n
        try:
            os.rename(longpath(n.path), longpath(dst))
        except OSError as e:
            self.done(s, False, f"{e.strerror or e} (folder in use?)")
            return n
        self.done(s, True)
        return Copy(NAS, dst, want, n.origin_url, n.remote, n.roots)

    # -- P52: where work happens. Never rewrites anything of yours. ---------

    def sync_work_copy(self, c: Copy, online: Optional[OnlineRepo]) -> None:
        git, repo, args = self.git, c.path, self.ctx.args
        owned = online is not None
        rs = self.remote_state(c, online)
        if rs is None:
            return
        if self.unrelated_history(c, rs):
            self.R.issue(P52, "unrelated", f"{c.label} shares no history with {self.ident.display} on GitHub - "
                                           f"probably an old name reused for a new repo. Left alone.",
                         f'point origin at the right repo: cd "{repo}"; git remote set-url origin <url>')
            return
        default = detect_default_branch(git, repo, online)
        self.fix_head_ref(c, default)
        branch, _head = head_state(git, repo)
        entries = status_entries(git, repo)
        busy = in_progress(repo)
        if busy:
            self.R.issue(P52, "busy", f"P52 copy is in the middle of {busy} - left alone",
                         f'finish or abort it in "{repo}"')
            work = [e for e in entries if not is_cache_path(e.path)]
            if work:
                self.R.dirty.append(P52)
                self.backup_extras_step(c, work, [], "wip", f"{len(work)} uncommitted file(s)", wip=True)
            return
        elsewhere = other_worktree_branches(git, repo)
        branches = {b.name: b for b in list_branches(git, repo)}
        # Branch tips as this pass began. Pushes/deletes are signed with them, so the apply
        # only does what the plan showed: a commit made while you read the plan waits.
        start = {name: b.sha for name, b in branches.items()}

        # 1) bring the checked-out branch up to date first
        ff_blocked = False
        head_diverged = False
        head_state_ = "none"
        if branch and branch in branches and branch not in elsewhere:
            head_state_, head_up = self.upstream_of(branches[branch], rs)
            up = rs.ref(head_up) if head_up else None
            if head_state_ == "ok" and up:
                a, bh = ahead_behind(git, repo, f"refs/heads/{branch}", up)
                head_diverged = a > 0 and bh > 0
                if bh > 0 and a == 0:
                    clobber = ff_would_clobber(git, repo, up, entries)
                    if clobber:
                        ff_blocked = True
                        self.R.issue(P52, "ff-blocked",
                                     f"{branch}: {bh} new commit(s) on GitHub, but your uncommitted changes to "
                                     f"{', '.join(clobber[:3])}{' ...' if len(clobber) > 3 else ''} are in the way",
                                     f'cd "{repo}"; git stash; git pull --ff-only; git stash pop   (or commit first)')
                    else:
                        s = self.R.step(P52, "ff", branch, f"P52: pull {bh} new commit(s) into {branch}")
                        if self.gate(s):
                            # --no-overwrite-ignore: never clobber an ignored file of yours (e.g. .env)
                            r = git.run(repo, "merge", "--ff-only", "--no-overwrite-ignore", "--quiet", up)
                            if not self.done(s, r.ok, r.why()):
                                ff_blocked = True

        # 2) old-script stamps
        expect_push: set[str] = set()
        stamp = StampInfo("none")
        if entries:
            names = [c.folder, to_kebab(c.folder)] + ([online.name, to_kebab(online.name)] if online else [])
            stamp = detect_stamps(git, repo, entries, self.ctx.templates, names)
        if stamp.kind == "stamp-only":
            if not owned:
                self.R.info("P52: old README/doc stamps on someone else's repo - left alone")
            elif args.stamps == "keep":
                self.R.info("P52: only change is the old README/doc stamps (kept, --stamps keep)")
            elif args.stamps == "discard":
                self.discard_stamps(c, entries, stamp)
            elif online is not None and online.archived:
                self.R.issue(P52, "archived", "old stamps not committed: repo is archived on GitHub")
            elif args.no_push:
                self.R.info("P52: old stamps not committed (--no-push)")
            elif not branch:
                self.R.issue(P52, "stamps", "old stamps not committed: P52 copy has a detached HEAD")
            elif branch not in branches:
                self.R.info("P52: old stamps not committed: the branch has no commits yet")
            elif ff_blocked or head_diverged:
                self.R.issue(P52, "stamps", f"old stamps not committed: {branch} isn't level with GitHub (see above)")
            elif head_state_ not in ("ok", "none"):
                self.R.issue(P52, "stamps", f"old stamps not committed: {branch} doesn't track a live GitHub branch")
            else:
                s = self.R.step(P52, "commit-stamps", f"{branch}:{short_digest(sorted(stamp.paths))}",
                                f"P52: commit the old README/doc stamps on {branch} ({', '.join(stamp.paths)})")
                if not self.apply:
                    expect_push.add(branch)
                elif self.gate(s):
                    r = git.run(repo, "add", "--", *stamp.paths)
                    if r.ok:
                        r = git.run(repo, "commit", "--quiet", "-m", STAMP_COMMIT_MESSAGE, "--", *stamp.paths)
                    self.done(s, r.ok, r.why())
                if self.apply:
                    branches = {b.name: b for b in list_branches(git, repo)}
                    entries = status_entries(git, repo)
                    if not entries:
                        stamp = StampInfo("none")

        # 3) every local branch
        diverged_refs: list[tuple[str, str]] = []
        handled_stale: set[str] = set()
        for name in sorted(branches):
            b = branches[name]
            tip = start.get(name, b.sha)
            if name in elsewhere:
                self.R.info(f"P52: {name} is checked out in another worktree - left alone")
                continue
            st, target = self.upstream_of(b, rs)
            if st == "other":
                continue
            up = rs.ref(target) if target else None
            if st in ("gone-seen", "gone-unseen"):
                if st == "gone-seen":
                    handled_stale.add(target)
                self.gone_branch(c, b, name == branch, default, st == "gone-seen", target, rs, online, tip)
                continue
            if st == "none":
                if not owned:
                    self.R.info(f"P52: branch {name} exists only locally (someone else's repo)")
                elif online.archived:  # type: ignore[union-attr]
                    self.R.issue(P52, "archived", f"branch {name} not published: repo is archived on GitHub")
                elif args.no_push or args.no_push_new_branches:
                    self.R.info(f"P52: branch {name} exists only locally (not pushed: flags)")
                else:
                    s = self.R.step(P52, "push-new", f"{name}@{tip}", f"P52: publish new branch {name} to GitHub")
                    if self.gate(s):
                        r = git.run(repo, "push", "--porcelain", "-u", "origin",
                                    f"refs/heads/{name}:refs/heads/{name}", timeout=GIT_TIMEOUT_NET)
                        self.done(s, r.ok, r.why())
                continue
            a, bh = ahead_behind(git, repo, f"refs/heads/{name}", up)  # type: ignore[arg-type]
            if name in expect_push and a == 0:
                a, bh = 1, 0                       # plan pass: the stamp commit, after any fast-forward
            if a == 0 and bh == 0:
                continue
            if a > 0 and bh == 0:
                if not owned:
                    self.R.issue(P52, "foreign-ahead",
                                 f"{name}: {a} commit(s) only on the P52 - it's someone else's repo, can't push",
                                 "fork it on GitHub and point origin at your fork")
                elif online.archived:  # type: ignore[union-attr]
                    self.R.issue(P52, "archived", f"{name}: {a} commit(s) not pushed - repo is archived on GitHub",
                                 "unarchive it on GitHub, then run again")
                elif args.no_push:
                    self.R.info(f"P52: {name} has {a} unpushed commit(s) (--no-push)")
                else:
                    s = self.R.step(P52, "push", f"{name}@{tip}", f"P52: push {a} commit(s) on {name} to GitHub")
                    if self.gate(s):
                        r = git.run(repo, "push", "--porcelain", "origin", f"refs/heads/{name}:refs/heads/{target}",
                                    timeout=GIT_TIMEOUT_NET)
                        if self.done(s, r.ok, r.why()) and not b.upstream:
                            git.run(repo, "branch", f"--set-upstream-to=origin/{target}", name)
                continue
            if a == 0 and bh > 0:
                if name == branch:
                    continue                       # handled in step 1
                s = self.R.step(P52, "ff", name, f"P52: pull {bh} new commit(s) into {name}")
                if self.gate(s):
                    new = ref_sha(git, repo, up)  # type: ignore[arg-type]
                    r = git.run(repo, "update-ref", "-m", "repo-sync: fast-forward", f"refs/heads/{name}",
                                new or "", b.sha) if new else GitResult(1, "", "upstream vanished")
                    self.done(s, r.ok, r.why())
                continue
            self.R.issue(P52, "diverged",
                         f"{name}: {a} commit(s) only on the P52 and {bh} only on GitHub",
                         f'cd "{repo}"; git switch {name}; git pull --rebase   (or merge), then run again')
            diverged_refs.append((f"refs/heads/{name}", b.sha))

        # 4) remote-tracking refs for branches that are gone from GitHub
        self.prune_stale(c, rs, handled_stale)

        # 5) anything that exists only here gets a (deduplicated) safety snapshot
        stashes = [(f"stash@{{{i}}}", sha) for i, sha in enumerate(stash_shas(git, repo))]
        wip_entries = [] if stamp.kind == "stamp-only" else [e for e in entries if not is_cache_path(e.path)]
        refs = diverged_refs + stashes
        if wip_entries:
            self.R.dirty.append(P52)
        if wip_entries or refs:
            what = []
            if wip_entries:
                what.append(f"{len(wip_entries)} uncommitted file(s)")
            if diverged_refs:
                what.append(f"{len(diverged_refs)} diverged branch(es)")
            if stashes:
                what.append(f"{len(stashes)} stash entr{'y' if len(stashes) == 1 else 'ies'}")
            self.backup_extras_step(c, wip_entries, refs, "wip", " + ".join(what), wip=True)

    def discard_stamps(self, c: Copy, entries: list[Entry], stamp: StampInfo) -> None:
        stamp_entries = [e for e in entries if e.path in stamp.paths]
        if not self.backup_extras_step(c, stamp_entries, [], "stamps", "the old stamps"):
            return
        s = self.R.step(c.base, "discard-stamps", "stamps:" + short_digest(sorted(stamp.paths)),
                        f"{c.label}: discard the old README/doc stamps")
        if self.gate(s):
            ok, err = True, ""
            for e in stamp_entries:
                if e.xy == "??":
                    try:
                        os.remove(longpath(c.path / e.path))
                    except OSError as ex:
                        ok, err = False, str(ex)
                else:
                    r = self.git.run(c.path, "checkout", "HEAD", "--", e.path)
                    if not r.ok:
                        ok, err = False, r.why()
            self.done(s, ok, err)

    def gone_branch(self, c: Copy, b: Branch, is_head: bool, default: Optional[str], seen: bool,
                    target: str, rs: "RemoteState", online: Optional[OnlineRepo], tip: str) -> None:
        git, repo, args = self.git, c.path, self.ctx.args
        if args.keep_gone_branches:
            self.R.info(f"P52: {b.name}'s GitHub branch is gone (kept, --keep-gone-branches)")
            return
        up_ref = rs.ref(target)
        has_up = seen and target in rs.tracking
        # The old tracking ref can be AHEAD of the local branch (fetched but never pulled):
        # its commits count too, or they'd vanish with it.
        tips = [f"refs/heads/{b.name}"] + ([up_ref] if has_up else [])
        unique = count_not_in(git, repo, tips, rs.live_refs)
        if unique < 0:
            self.R.issue(P52, "gone-branch", f"{b.name}: its GitHub branch is gone and its commits couldn't be checked - kept")
            return
        if not seen:
            # Tracks a GitHub branch that never showed up here: never pushed (e.g. you cloned
            # an empty repo) - or you pruned it yourself. Nothing unique -> leave it; else publish.
            if unique == 0:
                self.R.info(f"P52: {b.name} tracks origin/{target}, which isn't on GitHub (nothing unique in it)")
            elif online is None:
                self.R.info(f"P52: {b.name} exists only locally (someone else's repo)")
            elif online.archived:
                self.R.issue(P52, "archived", f"branch {b.name} not published: repo is archived on GitHub")
            elif args.no_push or args.no_push_new_branches:
                self.R.info(f"P52: {b.name} has {unique} commit(s) that aren't on GitHub (not pushed: flags)")
            else:
                s = self.R.step(P52, "push-new", f"{b.name}@{tip}",
                                f"P52: publish {b.name} ({unique} commit(s); its GitHub branch '{target}' doesn't exist)")
                if self.gate(s):
                    r = git.run(repo, "push", "--porcelain", "-u", "origin", f"refs/heads/{b.name}:refs/heads/{target}",
                                timeout=GIT_TIMEOUT_NET)
                    self.done(s, r.ok, r.why())
            return
        if is_head:
            extra = f" ({unique} commit(s) only here)" if unique else ""
            self.R.issue(P52, "gone-branch", f"{b.name} was deleted on GitHub but it's checked out on the P52{extra}",
                         f'cd "{repo}"; git switch {default or "main"}; then run again (it gets backed up + removed)')
            return
        refs = [(f"refs/heads/{b.name}", b.sha)] + ([(up_ref, rs.tracking[target])] if has_up else [])
        if unique > 0 and not self.backup_extras_step(c, [], refs, f"branch-{slug(b.name, 40)}",
                                                      f"branch {b.name} ({unique} commit(s) that aren't on GitHub)"):
            return
        how = "all its commits are still online" if unique == 0 else f"its {unique} commit(s) backed up"
        s = self.R.step(P52, "delete-branch", f"{b.name}@{tip}",
                        f"P52: delete branch {b.name} (deleted on GitHub; {how})")
        if self.gate(s):
            r = git.run(repo, "update-ref", "-d", f"refs/heads/{b.name}", b.sha)
            if r.ok and has_up:
                git.run(repo, "update-ref", "-d", up_ref, rs.tracking[target])
            self.done(s, r.ok, r.why())

    # -- NAS: an exact mirror of GitHub -------------------------------------

    def sync_mirror(self, c: Copy, online: Optional[OnlineRepo], default_hint: Optional[str] = None,
                    source: Optional[Path] = None) -> None:
        git, repo = self.git, c.path
        rs = self.remote_state(c, online, source)
        if rs is None:
            return
        if not rs.live:
            self.R.info("NAS: the GitHub repo is empty - nothing to mirror yet")
            return
        if self.unrelated_history(c, rs):
            self.R.issue(NAS, "unrelated", f"{c.label} shares no history with {self.ident.display} - probably an "
                                           f"old name reused for a new repo. Left alone.", f'look in "{repo}"')
            return
        if source is not None:
            default = default_hint
        else:
            default = detect_default_branch(git, repo, online) or default_hint
        if not default or default not in rs.remotes:
            self.R.issue(NAS, "no-default", f"NAS: can't find the default branch ({default}) on GitHub - left alone")
            return
        if source is None:
            self.fix_head_ref(c, default)
        busy = in_progress(repo)
        branch, head = head_state(git, repo)
        entries = status_entries(git, repo)
        branches = list_branches(git, repo)

        odd_dirs = [e.path for e in entries if os.path.isdir(longpath(repo / e.path))]
        if odd_dirs:
            self.R.issue(NAS, "nested", f"NAS copy has nested repos/submodule changes ({', '.join(odd_dirs[:3])}) "
                                        f"- not reset automatically", f'look in "{repo}"')
            return

        extra_refs: list[tuple[str, str]] = []
        fix_refs: list[tuple[Branch, str]] = []      # branch -> GitHub ref to match (reset or fast-forward)
        drop: list[Branch] = []                        # branches GitHub doesn't have
        ffs: list[tuple[Branch, str, int]] = []
        n_ahead = 0
        for b in branches:
            st, short = self.upstream_of(b, rs)
            up = rs.ref(short) if short else None
            if st == "ok" and up:
                a, bh = ahead_behind(git, repo, f"refs/heads/{b.name}", up)
                if a == 0 and bh == 0:
                    continue
                fix_refs.append((b, up))
                if a == 0:
                    ffs.append((b, up, bh))
                    continue
                n_ahead += 1
            else:
                drop.append(b)
            if count_not_in(git, repo, f"refs/heads/{b.name}", rs.live_refs) != 0:
                extra_refs.append((f"refs/heads/{b.name}", b.sha))
        detached = branch is None
        if detached and head and count_not_in(git, repo, head, rs.live_refs) != 0:
            extra_refs.append(("HEAD", head))          # commits made on a detached HEAD
        messy = bool(entries or n_ahead or drop or busy or detached)
        if not messy:
            for b, up, bh in ffs:
                s = self.R.step(NAS, "ff", b.name, f"NAS: pull {bh} new commit(s) into {b.name}")
                if self.gate(s):
                    if b.name == branch:
                        r = git.run(repo, "merge", "--ff-only", "--no-overwrite-ignore", "--quiet", up)
                    else:
                        new = ref_sha(git, repo, up)
                        r = git.run(repo, "update-ref", "-m", "repo-sync: fast-forward", f"refs/heads/{b.name}",
                                    new or "", b.sha) if new else GitResult(1, "", "upstream vanished")
                    self.done(s, r.ok, r.why())
            self.prune_stale(c, rs, set())
            return

        stamp = StampInfo("none")
        if entries:
            names = [c.folder, to_kebab(c.folder)] + ([online.name, to_kebab(online.name)] if online else [])
            stamp = detect_stamps(git, repo, entries, self.ctx.templates, names)
        what = []
        if entries:
            what.append("old README/doc stamps" if stamp.kind == "stamp-only" else f"{len(entries)} changed file(s)")
        if n_ahead:
            what.append(f"{n_ahead} branch(es) with commits not on GitHub")
        if drop:
            what.append(f"{len(drop)} branch(es) GitHub doesn't have")
        if busy:
            what.append(f"an unfinished {busy}")
        if detached:
            what.append("a detached HEAD")
        summary = ", ".join(what)
        stale_uniq = [(rs.ref(k), v) for k, v in sorted(rs.stale.items())
                      if count_not_in(git, repo, rs.ref(k), rs.live_refs) != 0]

        safe = True
        # Everything checkout --force / clean -f -d would destroy goes into the backup, whole
        # and uncapped - except the old stamps, byte-identical to the templates they came from.
        backup_entries = [] if stamp.kind == "stamp-only" else entries
        if busy:
            safe = self.full_backup_step(c, "nas-unfinished", f"NAS copy ({summary})")
        elif backup_entries or extra_refs or stale_uniq:
            safe = self.backup_extras_step(c, backup_entries, extra_refs + stale_uniq, "nas-extras",
                                           f"NAS-only stuff ({summary})")
        digest = short_digest(sorted((e.xy, e.path) for e in entries), detached, busy)
        s = self.R.step(NAS, "nas-reset", f"{default}:{digest}", f"NAS: reset to GitHub ({summary})")
        if not self.gate(s):
            return
        if not safe:
            self.done(s, False, "backup didn't complete, NAS copy left as it was")
            return
        ok, err = self.reset_mirror(repo, default, fix_refs, drop, busy, rs)
        self.done(s, ok, err)

    def reset_mirror(self, repo: Path, default: str, fix_refs: list[tuple[Branch, str]],
                     drop: list[Branch], busy: Optional[str], rs: "RemoteState") -> tuple[bool, str]:
        git = self.git
        if busy:
            for cmd in (["merge", "--abort"], ["rebase", "--abort"], ["cherry-pick", "--abort"],
                        ["revert", "--abort"], ["am", "--abort"], ["bisect", "reset"]):
                git.run(repo, *cmd)
        r = git.run(repo, "checkout", "--force", "-B", default, rs.ref(default))
        if not r.ok:
            return False, f"checkout failed: {r.why()}"
        git.run(repo, "branch", f"--set-upstream-to=origin/{default}", default)
        r = git.run(repo, "clean", "-f", "-d")
        if not r.ok:
            return False, f"clean failed: {r.why()}"
        for b, up in fix_refs:
            if b.name == default:
                continue
            new = ref_sha(git, repo, up)
            if new:
                r = git.run(repo, "update-ref", "-m", "repo-sync: match GitHub", f"refs/heads/{b.name}", new, b.sha)
                if not r.ok:
                    return False, f"couldn't reset {b.name}: {r.why()}"
        for b in drop:
            if b.name == default:
                continue
            r = git.run(repo, "update-ref", "-d", f"refs/heads/{b.name}", b.sha)
            if not r.ok:
                return False, f"couldn't remove branch {b.name}: {r.why()}"
        for k, sha in rs.stale.items():
            git.run(repo, "update-ref", "-d", rs.ref(k), sha)
        # stashes are left alone: they don't stop the copy matching GitHub
        left = status_entries(git, repo)
        if left:
            eg = ", ".join(e.path for e in left[:3])
            return False, (f"{len(left)} file(s) still differ after the reset ({eg}) - a file open elsewhere, or "
                           f"names Windows can't hold (differing only in case, or like aux/con/nul)")
        return True, ""

    # -- repos that must go (deleted online / --local-only archive) ---------

    def full_backup_step(self, c: Copy, reason: str, what: str, fp: Optional[str] = None) -> bool:
        s = self.R.step(c.base, "backup", reason, f"{c.label}: full backup of {what}")
        if not self.apply:
            return True
        try:
            fp = fp or fingerprint_full(self.git, c.path)
        except OSError as e:
            self.done(s, False, f"couldn't read the folder: {e}")
            return False
        ok, res = self.ctx.store.backup_full(self.git, c, self.ident.key, reason, fp)
        self.done(s, ok, res)
        if ok:
            self.R.backups.append(res)
        return ok

    def delete_all_copies(self, reason: str, why: str) -> None:
        if not self.ctx.deletes_allowed:
            self.R.issue(None, "brakes", f"{self.ident.display}: {why} - NOT deleted (too many deletions this run)",
                         "check the list, then run again with --allow-mass-delete")
            return
        if self.ctx.args.keep_gone and self.ident.kind == GONE:
            self.R.info(f"{why} - kept (--keep-gone)")
            return
        for c in self.ident.all_copies():
            fp_before = ""
            if self.apply:
                try:
                    fp_before = fingerprint_full(self.git, c.path)
                except OSError as e:
                    self.R.issue(c.base, "not-deleted", f"{c.label} kept: couldn't read all of it ({e})")
                    continue
            if not self.full_backup_step(c, reason, f"{c.path}", fp_before or None):
                self.R.issue(c.base, "not-deleted", f"{c.label} kept: backup failed")
                continue
            s = self.R.step(c.base, "delete-repo", c.folder, f"{c.label}: delete {c.path} ({why}; backed up first)")
            if not self.gate(s):
                continue
            try:
                unchanged = fingerprint_full(self.git, c.path) == fp_before
            except OSError:
                unchanged = False
            if not unchanged:
                self.done(s, False, "folder changed while it was being backed up - kept, run again")
                continue
            ok, err = delete_repo_folder(c.path)
            self.done(s, ok, err)

    # -- never been online ---------------------------------------------------

    def do_local_only(self) -> None:
        policy = self.ctx.args.local_only
        if policy == "keep":
            self.R.info("never been online - left alone (--local-only keep)")
            return
        if policy == "archive":
            self.delete_all_copies("never-online", "never been on GitHub (--local-only archive)")
            return
        if self.ident.new_repo is None:
            return                                   # reason already in issues
        p, n = self.ident.canonical[P52], self.ident.canonical[NAS]
        src = p or n
        if src is None:
            return
        git = self.git
        if not has_any_ref(git, src.path, "refs/heads"):
            self.R.issue(src.base, "empty", f"{src.label} has no commits yet - nothing to publish")
            return
        busy = in_progress(src.path)
        if busy:
            self.R.issue(src.base, "busy", f"{src.label} is in the middle of {busy} - not published yet")
            return
        if self.ctx.args.no_push:
            self.R.info("never been online - not published (--no-push)")
            return
        owner, name = self.ident.new_repo
        branch, _ = head_state(git, src.path)
        heads = {b.name: b.sha for b in list_branches(git, src.path)}
        first = branch if branch in heads else next((h for h in ("main", "master") if h in heads), sorted(heads)[0])
        stamp = StampInfo("none")
        entries = status_entries(git, src.path)
        if entries and self.ctx.args.stamps == "commit" and branch in heads:
            stamp = detect_stamps(git, src.path, entries, self.ctx.templates, [src.folder, to_kebab(src.folder), name])
        other, other_base = (n, NAS) if src is p else (p, P52)

        s_create = self.R.step(None, "create-online", f"{owner}/{name}", f"create PRIVATE repo {owner}/{name} on GitHub")
        s_stamp = None
        if stamp.kind == "stamp-only":
            s_stamp = self.R.step(src.base, "commit-stamps", f"{branch}:{short_digest(sorted(stamp.paths))}",
                                  f"{src.label}: commit the old README/doc stamps ({', '.join(stamp.paths)})")
        s_pub = self.R.step(src.base, "publish", "all:" + short_digest(sorted(heads.items())),
                            f"{src.label}: push {len(heads)} branch(es) + tags to {owner}/{name}")
        if not self.apply:
            # show what the NAS copy will need once this is on GitHub (= this local repo)
            if n is not None and self.ctx.nas_base is not None:
                try:
                    self.sync_mirror(n, None, default_hint=first, source=src.path)
                finally:
                    drop_refs(git, n.path, PREVIEW_NS)
            if other is None and self.ident.target.get(other_base) and (other_base == P52 or self.ctx.nas_base is not None):
                self.R.step(other_base, "clone", self.ident.target[other_base],
                            f"clone {owner}/{name} to the {other_base} as '{self.ident.target[other_base]}'")
            return
        # All-or-nothing: if any of it wasn't in the plan (e.g. you committed since), nothing
        # is created - no empty repos left behind.
        if not self.gate_all([s_create] + ([s_stamp] if s_stamp else []) + [s_pub]):
            return
        repo_obj, err = self.ctx.api.create_private_repo(owner, name, self.ctx.me,
                                                          f"Published by repo-sync from {src.folder}")
        if repo_obj is None:
            self.done(s_create, False, err)
            for st in [s_stamp, s_pub]:
                if st is not None:
                    st.status, st.error = "skipped", "repo couldn't be created"
            return
        self.done(s_create, True)
        self.ctx.online[repo_obj.key] = repo_obj
        self.ident.online, self.ident.url, self.ident.kind = repo_obj, repo_obj.url, ONLINE
        self.R.kind = ONLINE
        want = (src.remote.with_repo(repo_obj.owner, repo_obj.name)
                if src.remote is not None and src.remote.kind == "github" else repo_obj.url)
        r = git.run(src.path, "remote", "set-url" if src.origin_url else "add", "origin", want)
        if not r.ok:
            self.done(s_pub, False, f"couldn't set origin: {r.why()}")
            return
        src.origin_url, src.remote = want, parse_remote_url(want)
        if s_stamp is not None:
            r = git.run(src.path, "add", "--", *stamp.paths)
            if r.ok:
                r = git.run(src.path, "commit", "--quiet", "-m", STAMP_COMMIT_MESSAGE, "--", *stamp.paths)
            self.done(s_stamp, r.ok, r.why())
        r = git.run(src.path, "push", "--porcelain", "-u", "origin", f"refs/heads/{first}:refs/heads/{first}",
                    timeout=GIT_TIMEOUT_NET)
        if not r.ok:
            self.done(s_pub, False, r.why())
            return
        okd, errd = self.ctx.api.set_default_branch(repo_obj.owner, repo_obj.name, first)
        if not okd:
            self.R.info(f"couldn't set default branch to {first}: {errd}")
        repo_obj.default_branch = first
        r1 = git.run(src.path, "push", "--porcelain", "-u", "origin", "--all", timeout=GIT_TIMEOUT_NET)
        r2 = git.run(src.path, "push", "--porcelain", "origin", "--tags", timeout=GIT_TIMEOUT_NET)
        if not self.done(s_pub, r1.ok and r2.ok, (r1 if not r1.ok else r2).why()):
            return
        # From here it's a normal online repo.
        if src is p:
            self.sync_work_copy(p, repo_obj)
            if self.ctx.nas_base is not None:
                if n is not None:
                    self.sync_mirror(n, repo_obj, default_hint=first)
                elif not self.ident.copies[NAS]:
                    self.clone(NAS)
        else:
            self.sync_mirror(n, repo_obj, default_hint=first)  # type: ignore[arg-type]
            if p is None and not self.ident.copies[P52]:
                self.clone(P52)


# ---------------------------------------------------------------------------
# PASSES, OUTPUT, REPORTS
# ---------------------------------------------------------------------------

STEP_LABELS = [
    ("clone", "clone"),
    ("ff", "pull new commits (fast-forward)"),
    ("push", "push commits to GitHub"),
    ("push-new", "publish new local branches"),
    ("create-online", "create PRIVATE GitHub repos"),
    ("publish", "first push of those new repos"),
    ("commit-stamps", "commit old README/doc stamps"),
    ("discard-stamps", "discard old README/doc stamps"),
    ("retarget", "fix origin URL (moved/renamed online)"),
    ("rename-folder", "rename NAS folder to match P52"),
    ("nas-reset", "reset NAS copy to GitHub"),
    ("backup", "new backups"),
    ("delete-branch", "delete branch deleted on GitHub"),
    ("prune-refs", "forget branches deleted on GitHub"),
    ("delete-repo", "delete repo (backed up first)"),
]


def run_pass(ctx: Ctx, idents: list[Identity], apply: bool, planned: Optional[dict] = None) -> dict[str, Result]:
    results: dict[str, Result] = {}
    total = len(idents)
    label = "Applying" if apply else "Checking"
    out(f"{label} {total} repos{'' if apply else ' (fetching; nothing of yours changes)'} ...")
    started = time.time()
    ex = ThreadPoolExecutor(max_workers=max(1, ctx.args.jobs))
    try:
        futs = {ex.submit(Runner(ctx, i, apply, planned.get(i.key, set()) if planned is not None else None).process): i
                for i in idents}
        n = 0
        for fut in as_completed(futs):
            ident = futs[fut]
            res = fut.result()
            results[ident.key] = res
            n += 1
            if apply and (res.steps or res.issues):
                done = sum(1 for s in res.steps if s.status == "done")
                bad = sum(1 for s in res.steps if s.status in ("failed", "skipped"))
                flag = "  <-- NEEDS YOU" if res.issues else ""
                out(f"  [{n}/{total}] {res.display}: {done} done" + (f", {bad} not done" if bad else "") + flag)
            elif n % 50 == 0 or n == total:
                out(f"  ... {n}/{total} ({time.time() - started:.0f}s)")
    except KeyboardInterrupt:
        ctx.stop.set()
        out("\nStopping: repos already in progress finish their current git command, nothing else starts.")
        ex.shutdown(wait=True, cancel_futures=True)
        raise
    ex.shutdown(wait=True)
    return results


def planned_sigs(results: dict[str, Result]) -> dict[str, set]:
    return {k: {s.sig for s in r.steps} for k, r in results.items()}


def print_summary(results: dict[str, Result], title: str, apply: bool, examples: int = 5) -> None:
    out("")
    out("=" * 72)
    out(title)
    out("=" * 72)
    by_kind: dict[str, list[tuple[Result, Step]]] = {}
    for r in results.values():
        for s in r.steps:
            by_kind.setdefault(s.kind, []).append((r, s))
    any_steps = False
    for kind, text in STEP_LABELS:
        items = by_kind.get(kind, [])
        if not items:
            continue
        any_steps = True
        names = sorted({r.display for r, _ in items})
        eg = ", ".join(names[:examples]) + (f" (+{len(names) - examples} more)" if len(names) > examples else "")
        if apply:
            ok = sum(1 for _, s in items if s.status == "done")
            bad = len(items) - ok
            count = f"{ok:>4} done" + (f", {bad} not" if bad else "")
        else:
            count = f"{len(items):>4}"
        out(f"  {text + ' ':.<46} {count}   {eg}")
    in_sync = sum(1 for r in results.values()
                  if not r.steps and not r.issues and not r.dirty and r.kind in (ONLINE, FOREIGN))
    out(f"  {'already in agreement ':.<46} {in_sync:>4}")
    dirty = sorted(r.display for r in results.values() if r.dirty)
    if dirty:
        eg = ", ".join(dirty[:examples]) + (f" (+{len(dirty) - examples} more)" if len(dirty) > examples else "")
        out(f"  {'uncommitted work on the P52 (left alone) ':.<46} {len(dirty):>4}   {eg}")
    if not any_steps:
        out("  (no changes needed)")
    issues = [i for r in results.values() for i in r.issues]
    if issues:
        out("")
        out(f"NEEDS YOU ({len(issues)})  - nothing here was changed")
        disp = {r.key: r.display for r in results.values()}
        for i in sorted(issues, key=lambda x: (x.kind, x.key))[:60]:
            where = f" [{i.base}]" if i.base else ""
            out(f"  - {disp.get(i.key, i.key)}{where}: {i.message}")
            if i.hint:
                out(f"      -> {i.hint}")
        if len(issues) > 60:
            out(f"  ... and {len(issues) - 60} more in the report file")


def write_reports(state_dir: Path, stamp: str, mode: str, results: dict[str, Result], header: list[str],
                  notes: list[str], store: BackupStore) -> Path:
    os.makedirs(state_dir, exist_ok=True)
    md = state_dir / f"repo-sync-{stamp}-{mode}.md"
    js = state_dir / f"repo-sync-{stamp}-{mode}.json"
    disp = {r.key: r.display for r in results.values()}
    lines = [f"# repo-sync {mode} {stamp}", ""] + [f"    {h}" for h in header] + [""]
    issues = [i for r in results.values() for i in r.issues]
    lines.append(f"## NEEDS YOU ({len(issues)})")
    for i in sorted(issues, key=lambda x: (x.kind, x.key)):
        lines.append(f"- **{disp.get(i.key, i.key)}**{' [' + i.base + ']' if i.base else ''} ({i.kind}): {i.message}")
        if i.hint:
            lines.append(f"  - `{i.hint}`")
    dirty = sorted(r.display for r in results.values() if r.dirty)
    if dirty:
        lines += ["", f"## Uncommitted work on the P52 ({len(dirty)}) - left alone, backed up if new",
                  ", ".join(dirty)]
    lines += ["", "## Changes"]
    for r in sorted(results.values(), key=lambda x: x.display.lower()):
        if not r.steps and not r.infos:
            continue
        lines.append(f"### {r.display}")
        for s in r.steps:
            lines.append(f"- [{s.status}] {s.detail}" + (f" - {s.error}" if s.error else ""))
        for msg in r.infos:
            if "Traceback" in msg:
                lines += ["```", msg.rstrip(), "```"]
            else:
                lines.append(f"- note: {msg}")
    if store.created:
        lines += ["", "## Backups created"]
        lines += [f"- {f} ({human_bytes(sz)})" for f, sz in store.created]
    if notes:
        lines += ["", "## Scan notes"] + [f"- {n}" for n in notes]
    with open(md, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    data = {
        "tool": TOOL_VERSION, "stamp": stamp, "mode": mode, "header": header, "notes": notes,
        "backups_created": [{"file": f, "bytes": sz} for f, sz in store.created],
        "results": [
            {"key": r.key, "display": r.display, "kind": r.kind,
             "steps": [{"base": s.base, "kind": s.kind, "target": s.target, "detail": s.detail,
                        "status": s.status, "error": s.error} for s in r.steps],
             "issues": [{"base": i.base, "kind": i.kind, "message": i.message, "hint": i.hint} for i in r.issues],
             "infos": r.infos, "backups": r.backups, "dirty": r.dirty}
            for r in sorted(results.values(), key=lambda x: x.key)
        ],
    }
    with open(js, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    for pattern in ("repo-sync-*.md", "repo-sync-*.json"):
        old = sorted(state_dir.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)[40:]
        for p in old:
            try:
                p.unlink()
            except OSError:
                pass
    return md


class RunLock:
    """One run at a time. A live run refreshes the lock every few minutes (also while
    the YES prompt waits), so only a lock left behind by a dead run is taken over -
    and a run only ever removes its own lock."""
    STALE_AFTER = 30 * 60
    BEAT = 5 * 60

    def __init__(self, state_dir: Path) -> None:
        self.path = state_dir / "repo-sync.lock"
        self.token = f"{os.getpid()} {uuid.uuid4().hex}"
        self._stop = threading.Event()

    def acquire(self) -> None:
        os.makedirs(self.path.parent, exist_ok=True)
        for _ in range(3):
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                try:
                    age = time.time() - os.path.getmtime(self.path)
                except OSError:
                    continue                                 # it just vanished: try again
                if age > self.STALE_AFTER:
                    out(f"(taking over a lock left by a run that stopped {age / 60:.0f} min ago)")
                    try:
                        os.remove(self.path)
                    except OSError:
                        pass
                    continue
                raise SystemExit(f"Another repo_sync run is active (lock {self.path}, refreshed "
                                 f"{age / 60:.0f} min ago).\nIf you're sure it isn't, delete that file.") from None
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(self.token + "\n")
            threading.Thread(target=self._beat, daemon=True).start()
            return
        raise SystemExit(f"Couldn't take the lock {self.path}")

    def _beat(self) -> None:
        while not self._stop.wait(self.BEAT):
            try:
                os.utime(self.path, None)
            except OSError:
                pass

    def release(self) -> None:
        self._stop.set()
        try:
            with open(self.path, encoding="utf-8") as f:
                mine = f.read().strip() == self.token
            if mine:
                os.remove(self.path)
        except OSError:
            pass


def load_templates(docs_base: Path) -> dict[str, bytes]:
    t: dict[str, bytes] = {}
    for name in STAMP_DOCS:
        try:
            with open(longpath(docs_base / name), "rb") as f:
                t[name] = f.read()
        except OSError:
            pass
    return t


def count_top_level_repos(base: Path) -> int:
    try:
        return sum(1 for e in os.scandir(longpath(base))
                   if e.name not in SKIP_DIR_NAMES and e.is_dir() and os.path.isdir(longpath(Path(e.path) / ".git")))
    except OSError:
        return 0


def ident_matches(ident: Identity, patterns: list[str]) -> bool:
    names = {ident.key.lower(), ident.display.lower()}
    if ident.online is not None:
        names |= {ident.online.full.lower(), ident.online.name.lower()}
    names |= {c.folder.lower() for c in ident.all_copies()}
    names |= {t.lower() for t in ident.target.values() if t}
    return any(fnmatch.fnmatchcase(n, p.lower()) for p in patterns for n in names)


# ---------------------------------------------------------------------------
# COMMAND: sync
# ---------------------------------------------------------------------------


def cmd_sync(args: argparse.Namespace) -> int:
    stamp = run_stamp()
    git = Git(find_git())
    gv = git.version()
    if gv < (2, 30):
        out(f"WARNING: git {'.'.join(map(str, gv))} is old; 2.30+ recommended.")
    p52_base = Path(args.p52_base)
    if not os.path.isdir(longpath(p52_base)):
        raise SystemExit(f"P52 folder {p52_base} not found (--p52-base).")
    nas_base: Optional[Path] = None
    if not args.skip_nas:
        nas_base = Path(args.nas_base)
        if not os.path.isdir(longpath(nas_base)):
            hint = ""
            if os.path.isdir(longpath(nas_base.parent)) and count_top_level_repos(nas_base.parent):
                hint = (f"\n  {nas_base.parent} has {count_top_level_repos(nas_base.parent)} repos in it - "
                        f"if that's where your NAS repos live: --nas-base \"{nas_base.parent}\"")
            raise SystemExit(f"NAS folder {nas_base} not found. Is the NAS mapped? (repo-sync.ps1 maps Z: for you)"
                             f"{hint}\n  To sync only P52 <-> GitHub, add --skip-nas.")
    state_dir = Path(args.state_dir) if args.state_dir else Path(__file__).resolve().parent / "repo-sync-logs"
    lock = RunLock(state_dir)
    lock.acquire()
    try:
        return _sync(args, git, gv, stamp, p52_base, nas_base, state_dir)
    finally:
        lock.release()


def _sync(args: argparse.Namespace, git: Git, gv: tuple, stamp: str, p52_base: Path,
          nas_base: Optional[Path], state_dir: Path) -> int:
    token, token_src = find_token(git)
    api = GitHubApi(token)
    me = api.me()
    if args.owners:
        owners = [o.strip() for o in args.owners.split(",") if o.strip()]
    elif OWNERS:
        owners = list(OWNERS)
    else:
        try:
            owners = [me] + [o for o in api.my_orgs() if o.lower() != me.lower()]
        except ApiError as e:
            out(f"WARNING: couldn't list your orgs ({e}); token needs read:org. Using only {me}.")
            owners = [me]
    if me.lower() not in [o.lower() for o in owners]:
        owners = [me] + owners
    if args.new_repo_owner and args.new_repo_owner.lower() not in [o.lower() for o in owners]:
        raise SystemExit(f"--new-repo-owner {args.new_repo_owner} isn't one of your accounts: {', '.join(owners)}")

    backup_root = Path(args.backup_root) if args.backup_root else None
    store = BackupStore(backup_root, stamp, dry_run=True)
    header = [
        f"repo-sync {TOOL_VERSION}   {utc_now().strftime('%Y-%m-%d %H:%M UTC')}",
        f"git {'.'.join(map(str, gv))}   GitHub: {me} (token from {token_src})",
        f"accounts: {', '.join(owners)}",
        f"P52: {p52_base}   NAS: {nas_base or '(skipped)'}   backups: {backup_root or '(none)'}",
        f"never-online repos: {args.local_only}   old stamps: {args.stamps}",
    ]
    for h in header:
        out(h)
    if store.error:
        out(f"WARNING: {store.error} - anything that needs a backup first will be skipped.")

    out("Listing your repos on GitHub ...")
    online: dict[str, OnlineRepo] = {}
    unlisted: set[str] = set()
    for owner in owners:
        try:
            for r in api.list_repos(owner, me):
                online[r.key] = r
        except ApiError as e:
            unlisted.add(owner.lower())
            out(f"  WARNING: couldn't list {owner}'s repos ({e}). Nothing of {owner}'s will be deleted this run.")
    if not online and not unlisted:
        raise SystemExit("GitHub listed zero repos for all your accounts - wrong token? Stopping.")
    out(f"  {len(online)} repos across {len(owners) - len(unlisted)} account(s)")

    out("Scanning the P52 ...")
    p52_copies, p52_names, notes = scan_base(git, P52, p52_base, args.jobs)
    out(f"  {len(p52_copies)} repos in {p52_base}")
    nas_copies: list[Copy] = []
    nas_names: set = set()
    if nas_base is not None:
        out("Scanning the NAS ...")
        nas_copies, nas_names, n2 = scan_base(git, NAS, nas_base, args.jobs)
        notes += n2
        out(f"  {len(nas_copies)} repos in {nas_base}")
        parent_count = count_top_level_repos(nas_base.parent) if nas_base.parent != nas_base else 0
        if not nas_copies and len(p52_copies) >= 10 and not args.allow_empty_nas:
            hint = (f"\n{nas_base.parent} has {parent_count} repos - if those are your NAS copies, use "
                    f"--nas-base \"{nas_base.parent}\"") if parent_count else ""
            raise SystemExit(f"No repos found in {nas_base}.{hint}\n"
                             f"If the NAS really is empty and you want it filled, add --allow-empty-nas.")
        if parent_count >= 10 and nas_copies:
            msg = (f"{nas_base.parent} ALSO has {parent_count} repos next to {nas_base.name} - old copies? "
                   f"'repo_sync.py dupes --root \"{nas_base.parent}\"' shows which hold nothing new.")
            out(f"  heads-up: {msg}")
            notes.append(msg)

    # Make sure git itself can read a private repo before anything runs in parallel: Git
    # Credential Manager gets one chance to ask, and a missing login can never be mistaken
    # for "this repo was deleted on GitHub".
    probe_url = next((c.origin_url for c in p52_copies + nas_copies
                      if c.remote and c.remote.kind == "github" and c.remote.key in online
                      and online[c.remote.key].private), None)
    if probe_url is None:
        probe_url = next((r.url for r in sorted(online.values(), key=lambda r: r.key) if r.private), None)
    if probe_url:
        r = git.run(None, "ls-remote", probe_url, "HEAD", timeout=GIT_TIMEOUT_NET, interactive=not args.yes)
        if not r.ok:
            raise SystemExit(f"git can't read a private repo ({redact_url(probe_url)}): {r.why()}\n"
                             f"Sign git in once, then run again (nothing was changed):\n"
                             f"  git ls-remote {redact_url(probe_url)}")

    templates = load_templates(Path(args.docs_base))
    ctx = Ctx(git=git, api=api, args=args, me=me, owners=owners, owned={o.lower() for o in owners},
              unlisted=unlisted, online=online, store=store, templates=templates, p52_base=p52_base,
              nas_base=nas_base, names_present={P52: p52_names, NAS: nas_names})

    out("Working out what every folder is (moved repos, no-origin folders) ...")
    idents_map = classify(ctx, p52_copies + nas_copies, args.jobs)
    plan_targets(ctx, idents_map)
    idents = list(idents_map.values())
    if args.only:
        idents = [i for i in idents if ident_matches(i, args.only)]
    ignore = IGNORE + (args.ignore or [])
    if ignore:
        idents = [i for i in idents if not ident_matches(i, ignore)]
    idents.sort(key=lambda i: ident_sort_key(ctx, i))
    kinds: dict[str, int] = {}
    for i in idents:
        kinds[i.kind] = kinds.get(i.kind, 0) + 1
    moved = sum(1 for i in idents if i.moved_from)
    out(f"  {kinds.get(ONLINE, 0)} yours online ({moved} moved/renamed), {kinds.get(LOCAL_ONLY, 0)} never online, "
        f"{kinds.get(FOREIGN, 0)} third-party, {kinds.get(GONE, 0)} deleted online, {kinds.get(SKIPPED, 0)} skipped")

    deletions = sum(len(i.all_copies()) for i in idents
                    if i.kind == GONE or (i.kind == LOCAL_ONLY and args.local_only == "archive"))
    if deletions > args.max_deletes and not args.allow_mass_delete:
        ctx.deletes_allowed = False
        out(f"  BRAKES ON: {deletions} repo folders would be deleted (limit {args.max_deletes}); none will be.")

    plan = run_pass(ctx, idents, apply=False)
    print_summary(plan, "PLAN" + ("  (dry run)" if args.dry_run else ""), apply=False)
    report = write_reports(state_dir, stamp, "plan", plan, header, notes, store)
    out(f"\nFull plan: {report}")
    if args.json_report:
        shutil.copyfile(report.with_suffix(".json"), args.json_report + ".plan.json")
    has_steps = any(r.steps for r in plan.values())
    if args.dry_run:
        return 0
    if not has_steps:
        out("Nothing to change.")
        return 0
    if not args.yes:
        try:
            ans = input("\nType YES to apply this plan (anything else cancels): ")
        except EOFError:
            ans = ""
        if ans.strip() != "YES":
            out("Cancelled. Nothing was changed.")
            return 0

    ctx.store = BackupStore(backup_root, stamp, dry_run=False)
    if ctx.store.error:
        out(f"WARNING: {ctx.store.error} - steps that need a backup first will be skipped.")
    applied = run_pass(ctx, idents, apply=True, planned=planned_sigs(plan))
    print_summary(applied, "DONE", apply=True)
    if ctx.store.created:
        total = sum(sz for _, sz in ctx.store.created)
        out(f"\nBackups: {len(ctx.store.created)} new ({human_bytes(total)}) in {Path(str(backup_root)) / stamp}"
            f"   ({ctx.store.reused} already existed, not duplicated)")
    report = write_reports(state_dir, stamp, "apply", applied, header, notes, ctx.store)
    out(f"Full report: {report}")
    if args.json_report:
        shutil.copyfile(report.with_suffix(".json"), args.json_report)
    failed = any(s.status == "failed" for r in applied.values() for s in r.steps)
    return 1 if failed else 0


# ---------------------------------------------------------------------------
# COMMAND: dupes  (clear out copies that hold nothing the live repos don't)
# ---------------------------------------------------------------------------


def _real(p: Path | str) -> str:
    """One spelling per folder: resolves symlinks, junctions, Z:\\ vs \\\\nas\\share, case."""
    try:
        return os.path.normcase(os.path.realpath(str(p)))
    except OSError:
        return os.path.normcase(os.path.abspath(str(p)))


def _same_folder(a: Path, b: Path) -> bool:
    if _real(a) == _real(b):
        return True
    try:
        return os.path.samefile(longpath(a), longpath(b))
    except OSError:
        return False


def find_repos(roots: list[Path], max_depth: int, exclude: set) -> list[Path]:
    """Repo folders under roots (not descending into repos), one entry per real
    folder even if two roots/links reach it; `exclude` holds real paths."""
    found: dict[str, Path] = {}
    skip = CACHE_DIR_NAMES | {"$RECYCLE.BIN", "System Volume Information", "#recycle", "@eaDir", "@Recycle", ".git"}
    for root in roots:
        stack = [(root, 0)]
        while stack:
            d, depth = stack.pop()
            try:
                entries = list(os.scandir(longpath(d)))
            except OSError:
                continue
            if any(e.name == ".git" and e.is_dir(follow_symlinks=False) for e in entries):
                real = _real(d)
                if real not in exclude and real not in found:
                    found[real] = d
                continue
            if depth >= max_depth:
                continue
            for e in entries:
                try:
                    is_dir = e.is_dir(follow_symlinks=False)
                except OSError:
                    continue
                if is_dir and e.name not in skip and TOMBSTONE_SUFFIX not in e.name:
                    stack.append((d / e.name, depth + 1))
    return sorted(found.values(), key=lambda p: str(p).lower())


def all_commits_present(git: Git, repo: Path, shas: list[str]) -> bool:
    if not shas:
        return True
    r = git.run(repo, "cat-file", "--batch-check", input_text="".join(f"{s}\n" for s in shas))
    lines = [ln for ln in r.out.splitlines() if ln.strip()]
    return r.ok and len(lines) == len(shas) and not any(ln.rstrip().endswith("missing") for ln in lines)


def ignored_files(git: Git, repo: Path) -> list[str]:
    """Ignored files that matter (e.g. .env); ignored cache folders left out."""
    r = git.run(repo, "status", "--porcelain=v1", "-z", "--ignored=traditional", "--untracked-files=normal")
    files: list[str] = []
    for item in r.out.split("\x00"):
        if not item.startswith("!! "):
            continue
        rel = item[3:].rstrip("/")
        if item.endswith("/") and rel.split("/")[-1] in CACHE_DIR_NAMES:
            continue
        full = repo / rel
        if os.path.isdir(longpath(full)):
            files += [f"{rel}/{sub}" for sub, _ in iter_worktree_files(full)]
        else:
            files.append(rel)
    return files


def custom_hooks(repo: Path) -> list[str]:
    d = repo / ".git" / "hooks"
    try:
        return sorted(e.name for e in os.scandir(longpath(d)) if e.is_file() and not e.name.endswith(".sample"))
    except OSError:
        return []


def _same_file(a: Path, b: Path) -> bool:
    try:
        return (os.path.getsize(longpath(a)) == os.path.getsize(longpath(b))
                and sha256_file(a) == sha256_file(b))
    except OSError:
        return False


def folder_size(p: Path) -> int:
    total = 0
    for dirpath, _, filenames in os.walk(longpath(p)):
        for fn in filenames:
            try:
                total += os.path.getsize(os.path.join(dirpath, fn))
            except OSError:
                pass
    return total


@dataclass
class DupeInfo:
    path: Path
    fp: str = ""
    tips: list = field(default_factory=list)       # every commit any ref, HEAD, reflog or stash points at
    tags: dict = field(default_factory=dict)       # tag name -> object
    dirty: int = 0
    ignored: list = field(default_factory=list)
    hooks: list = field(default_factory=list)
    size: int = 0
    mtime: float = 0.0
    contained_in: Optional[Path] = None
    why_kept: str = ""
    error: str = ""


def cmd_dupes(args: argparse.Namespace) -> int:
    git = Git(find_git())
    roots = [Path(r) for r in args.root]
    for r in roots:
        if not os.path.isdir(longpath(r)):
            raise SystemExit(f"--root {r} not found")
    live_bases = [Path(args.p52_base)] + ([] if args.skip_nas else [Path(args.nas_base)])
    live: list[Path] = []
    for b in live_bases:
        if os.path.isdir(longpath(b)):
            live += [c.path for c in scan_base(git, "live", b, args.jobs)[0]]
    exclude = {_real(p) for p in live}
    out(f"Live repos (never touched here): {len(live)} in "
        f"{', '.join(str(b) for b in live_bases if os.path.isdir(longpath(b)))}")
    cands = find_repos(roots, args.depth, exclude)
    out(f"Repo copies found under {', '.join(map(str, roots))}: {len(cands)}")
    if not cands:
        return 0
    out("Indexing the live repos by history ...")
    index: dict[str, list[Path]] = {}
    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as ex:
        for p, rs_ in ex.map(lambda p: (p, root_commits(git, p)), live):
            for rt in rs_:
                index.setdefault(rt, []).append(p)
    live_cache: dict[str, tuple[list[str], dict]] = {}
    cache_lock = threading.Lock()

    def live_facts(lp: Path) -> tuple[list[str], dict]:
        """(exclusions = every ref tip + HEAD, tags) of a live repo; reflogs don't count."""
        key = _real(lp)
        with cache_lock:
            if key in live_cache:
                return live_cache[key]
        r = git.run(lp, "for-each-ref", "--format=%(refname)%00%(objectname)")
        tips, tags = [], {}
        for line in r.out.splitlines():
            name, _, sha = line.partition("\x00")
            if name.startswith("refs/repo-sync/"):
                continue
            tips.append(sha)
            if name.startswith("refs/tags/"):
                tags[name] = sha
        head = ref_sha(git, lp, "HEAD")
        if head:
            tips.append(head)
        with cache_lock:
            live_cache[key] = (tips, tags)
        return tips, tags

    def examine(p: Path) -> DupeInfo:
        d = DupeInfo(p)
        try:
            d.fp = fingerprint_full(git, p)
            r = git.run(p, "rev-list", "--exclude=refs/repo-sync/*", "--all", "--reflog", "--no-walk")
            if not r.ok:
                raise RepoError(f"couldn't list its commits: {r.why()}")
            d.tips = sorted(set(r.out.split()))
            t = git.run(p, "for-each-ref", "--format=%(refname)%00%(objectname)", "refs/tags")
            d.tags = dict(line.split("\x00", 1) for line in t.out.splitlines() if "\x00" in line)
            d.dirty = len(status_entries(git, p))
            d.ignored = ignored_files(git, p)
            d.hooks = custom_hooks(p)
            d.size = folder_size(p)
            d.mtime = os.path.getmtime(longpath(p))
            if d.dirty:
                d.why_kept = f"{d.dirty} uncommitted/untracked file(s)"
                return d
            if not d.tips and not d.tags:
                return d                                    # no commits at all: judged below
            live_cands = sorted({lp for rt in root_commits(git, p) for lp in index.get(rt, [])}, key=str)
            if not live_cands:
                d.why_kept = "no live repo shares its history"
                return d
            for lp in live_cands:
                excl, ltags = live_facts(lp)
                if not all_commits_present(git, lp, d.tips) or count_not_in(git, lp, d.tips, excl) != 0:
                    d.why_kept = f"has commits {lp.name} doesn't have on any branch or tag"
                    continue
                if any(ltags.get(name) != obj for name, obj in d.tags.items()):
                    d.why_kept = f"has tags {lp.name} doesn't have"
                    continue
                if not all(_same_file(p / rel, lp / rel) for rel in d.ignored):
                    d.why_kept = f"has ignored files (e.g. {d.ignored[0]}) that differ from {lp.name}"
                    continue
                if not all(_same_file(p / ".git" / "hooks" / h, lp / ".git" / "hooks" / h) for h in d.hooks):
                    d.why_kept = f"has git hooks {lp.name} doesn't"
                    continue
                d.contained_in = lp
                return d
        except (RepoError, OSError) as e:
            d.error = str(e)
            d.why_kept = f"couldn't check: {e}"
        return d

    out("Checking each copy (hashing files; can take a while on the NAS) ...")
    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as ex:
        infos = list(ex.map(examine, cands))

    deletable: dict[Path, str] = {}
    keeper_of: dict[Path, Path] = {}
    for d in infos:
        if d.error:
            continue
        if d.contained_in is not None:
            deletable[d.path] = f"everything in it is already in {d.contained_in}"
        elif not d.tips and not d.tags and d.dirty == 0 and not d.ignored and not d.hooks:
            deletable[d.path] = "empty repo, nothing inside"
    groups: dict[str, list[DupeInfo]] = {}
    for d in infos:
        if not d.error:
            groups.setdefault(d.fp, []).append(d)
    for members in groups.values():
        if len(members) < 2:
            continue
        keep = min(members, key=lambda x: (len(x.path.parts), len(str(x.path)), x.mtime))
        for m in members:
            if m is not keep and not _same_folder(m.path, keep.path) and m.path not in deletable:
                deletable[m.path] = f"identical to {keep.path}"
                keeper_of[m.path] = keep.path

    reclaim = sum(d.size for d in infos if d.path in deletable)
    out("")
    out(f"HOLD NOTHING NEW - safe to delete ({len(deletable)}, {human_bytes(reclaim)}):")
    for d in infos:
        if d.path in deletable:
            out(f"  - {d.path}  ({human_bytes(d.size)})  {deletable[d.path]}")
    kept = [d for d in infos if d.path not in deletable]
    out(f"HAVE SOMETHING UNIQUE - kept ({len(kept)}):")
    for d in kept:
        out(f"  - {d.path}  ({human_bytes(d.size)})  {d.why_kept or 'unique'}")
    if not args.apply:
        out("\nDry run. Add --apply to delete the 'hold nothing new' copies.")
        return 0
    if not deletable:
        return 0
    if not args.yes:
        try:
            ans = input(f"\nType YES to delete those {len(deletable)} copies (anything else cancels): ")
        except EOFError:
            ans = ""
        if ans.strip() != "YES":
            out("Cancelled. Nothing deleted.")
            return 0
    by_path = {d.path: d for d in infos}
    failed = 0
    for path in deletable:
        keeper = keeper_of.get(path)
        if keeper is not None and (not os.path.isdir(longpath(keeper / ".git")) or _same_folder(path, keeper)):
            out(f"  skipped (the copy it matches is gone or is the same folder): {path}")
            continue
        src = by_path[path].contained_in
        if src is not None and not os.path.isdir(longpath(src / ".git")):
            out(f"  skipped (the live repo it relies on is gone): {path}")
            continue
        try:
            changed = fingerprint_full(git, path) != by_path[path].fp
        except OSError:
            changed = True
        if changed:
            out(f"  skipped (changed since the check): {path}")
            continue
        ok, err = delete_repo_folder(path)
        out(f"  {'deleted' if ok else 'NOT deleted'}: {path}" + (f" ({err})" if err else ""))
        failed += 0 if ok else 1
    return 1 if failed else 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="repo_sync.py",
        description="Keep GitHub (the truth), the P52 and the NAS in agreement. "
                    "Default command is 'sync': plan -> type YES -> apply.")
    p.add_argument("--version", action="version", version=f"repo-sync {TOOL_VERSION}")
    sub = p.add_subparsers(dest="cmd")

    def common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--p52-base", default=DEFAULT_P52_BASE, help=f"your repos on the P52 (default {DEFAULT_P52_BASE})")
        sp.add_argument("--nas-base", default=DEFAULT_NAS_BASE, help=f"repos on the NAS (default {DEFAULT_NAS_BASE})")
        sp.add_argument("--skip-nas", action="store_true", help="leave the NAS out (P52 <-> GitHub only)")
        sp.add_argument("--jobs", type=int, default=DEFAULT_JOBS, help=f"repos worked on at once (default {DEFAULT_JOBS})")

    s = sub.add_parser("sync", help="plan -> YES -> apply (the default)")
    common(s)
    s.add_argument("--backup-root", default=DEFAULT_BACKUP_ROOT, help=f"default {DEFAULT_BACKUP_ROOT}")
    s.add_argument("--docs-base", default=DEFAULT_DOCS_BASE, help="folder with the old stamp templates")
    s.add_argument("--state-dir", default="", help="plans/reports/lock (default: repo-sync-logs next to this script)")
    s.add_argument("--dry-run", action="store_true", help="show the plan and stop")
    s.add_argument("--yes", action="store_true", help="don't ask, apply the plan (for scheduled runs)")
    s.add_argument("--only", action="append", default=[], metavar="PATTERN",
                   help="only repos matching (owner/name, folder, local:folder; wildcards ok). Repeatable")
    s.add_argument("--ignore", action="append", default=[], metavar="PATTERN", help="skip matching repos. Repeatable")
    s.add_argument("--owners", default="", help="comma list of your accounts/orgs (default: you + your orgs)")
    s.add_argument("--new-repo-owner", default="", help="account/org for never-online repos (default: you)")
    s.add_argument("--local-only", choices=["push", "archive", "keep"], default="push",
                   help="never-online repos: push = new PRIVATE repo (default); archive = back up + delete; keep = list")
    s.add_argument("--stamps", choices=["commit", "discard", "keep"], default="commit",
                   help="old-script README/doc stamps when they're the only change (default commit + push)")
    s.add_argument("--no-push", action="store_true", help="never push or create anything online")
    s.add_argument("--no-push-new-branches", action="store_true", help="don't publish local-only branches")
    s.add_argument("--keep-gone-branches", action="store_true", help="keep P52 branches deleted on GitHub")
    s.add_argument("--keep-gone", action="store_true", help="keep local copies of repos deleted on GitHub")
    s.add_argument("--no-rename-nas", action="store_true", help="don't rename NAS folders to match the P52")
    s.add_argument("--max-deletes", type=int, default=DEFAULT_MAX_DELETES,
                   help=f"repo folders that may be deleted in one run (default {DEFAULT_MAX_DELETES})")
    s.add_argument("--allow-mass-delete", action="store_true", help="release the brakes (after checking the plan)")
    s.add_argument("--allow-empty-nas", action="store_true", help="the NAS folder really is empty: fill it")
    s.add_argument("--json-report", default="", help=argparse.SUPPRESS)

    d = sub.add_parser("dupes", help="find (and with --apply delete) repo copies that hold nothing new")
    common(d)
    d.add_argument("--root", action="append", required=True, help="folder to search for copies. Repeatable")
    d.add_argument("--depth", type=int, default=3, help="how deep to look under each root (default 3)")
    d.add_argument("--apply", action="store_true", help="delete them (asks for YES)")
    d.add_argument("--yes", action="store_true", help="don't ask")
    return p


def main(argv: Optional[list] = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in ("sync", "dupes", "-h", "--help", "--version"):
        argv.insert(0, "sync")
    args = build_parser().parse_args(argv)
    try:
        if args.cmd == "dupes":
            return cmd_dupes(args)
        return cmd_sync(args)
    except KeyboardInterrupt:
        out("\nStopped by you.")
        return 130
    except ApiError as e:
        out(f"\nGitHub API problem: {e}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
