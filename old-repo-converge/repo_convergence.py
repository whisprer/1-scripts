#!/usr/bin/env python3
"""
repo_convergence_v2.py

Converges Git repositories across:
    - Local working tree: D:/code
    - NAS mirror:         Z:/GitHub-Repos
    - GitHub (user + org repos)

Primary goals:
    - Enforce lower-case kebab-case folder naming.
    - Optionally rename non-kebab GitHub repositories to kebab-case.
    - Converge duplicate local folders for the same logical repository into a
      single canonical folder per base (D and Z) without deleting anything.
    - Preserve safety by archiving merged/renamed surplus folders instead of
      deleting them, and by backing up overwritten files during merges.
    - Ensure required core documents exist from templates.
    - Ensure README.md has a standardised, idempotent managed header.

Default behaviour is DRY-RUN. Pass --apply to make changes.

Dependencies:
    - Python 3.8+
    - requests
    - git available either on PATH or at the configured fallback path
"""

from __future__ import annotations

import argparse
import filecmp
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote

import requests


# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------

DEFAULT_CODE_BASE = Path(r"D:/code")
DEFAULT_NAS_BASE = Path(r"Z:/GitHub-Repos")
DEFAULT_DOCS_BASE = Path(r"D:/code/1-git-docs")
ARCHIVE_DIR_NAME = ".repo-convergence-archive"
PLAN_FILENAME = "repo_convergence_plan.json"
README_HEADER_START = "<!-- repo-convergence:readme-header:start -->"
README_HEADER_END = "<!-- repo-convergence:readme-header:end -->"
README_LANGUAGE_META_RE = re.compile(r"<!--\s*repo-convergence:language=(.*?)\s*-->", re.IGNORECASE)
README_DEFAULT_LANGUAGE = "FILL_ME"
README_OLD_MARKER = "[README.md]"
GITHUB_API = "https://api.github.com"
TOKEN_ENV_CANDIDATES = [
    "GITHUB_PAT",
    "GH_RENAME_PAT",
    "GITHUB_TOKEN",
]
DOC_TEMPLATE_FILES = [
    "CHANGELOG.md",
    "CODE_OF_CONDUCT.md",
    "CONTRIBUTING.md",
    "LICENSE.md",
    "SECURITY.md",
]
GIT_FALLBACKS = [
    os.environ.get("REPO_CONVERGENCE_GIT", "").strip(),
    shutil.which("git") or "",
    r"C:\Program Files\Git\cmd\git.exe",
    r"C:\Program Files\Git\bin\git.exe",
]


# ---------------------------------------------------------------------------
# SMALL UTILITIES
# ---------------------------------------------------------------------------


def log(*parts: object) -> None:
    print(" ".join(str(p) for p in parts))


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def get_git_executable() -> str:
    for candidate in GIT_FALLBACKS:
        if candidate and Path(candidate).exists():
            return candidate
    git_on_path = shutil.which("git")
    if git_on_path:
        return git_on_path
    raise SystemExit("Could not find git. Set REPO_CONVERGENCE_GIT or install Git.")


GIT_EXE = get_git_executable()


def run_git(path: Optional[Path], *args: str, check: bool = False) -> subprocess.CompletedProcess:
    cmd = [GIT_EXE]
    if path is not None:
        cmd.extend(["-C", str(path)])
    cmd.extend(args)
    return subprocess.run(
        cmd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=check,
    )


def to_kebab(name: str) -> str:
    name = re.sub(r"\.git$", "", name, flags=re.IGNORECASE)
    name = re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", name)
    name = re.sub(r"[_\s]+", "-", name)
    name = re.sub(r"[^a-zA-Z0-9\-]", "-", name)
    name = re.sub(r"-{2,}", "-", name)
    return name.lower().strip("-")


def is_git_repo(path: Path) -> bool:
    return (path / ".git").is_dir()


def safe_case_rename(path: Path, new_path: Path) -> None:
    if path == new_path:
        return
    if path.parent == new_path.parent and path.name.lower() == new_path.name.lower() and path.name != new_path.name:
        temp = path.with_name(f"{new_path.name}__tmp_case_adjust__{utc_stamp()}")
        path.rename(temp)
        temp.rename(new_path)
    else:
        path.rename(new_path)


def ensure_dir(path: Path, apply_changes: bool) -> None:
    if apply_changes:
        path.mkdir(parents=True, exist_ok=True)


def get_token() -> str:
    for env_name in TOKEN_ENV_CANDIDATES:
        token = os.environ.get(env_name, "").strip()
        if token:
            return token
    raise SystemExit(
        "No GitHub token found. Set one of: " + ", ".join(TOKEN_ENV_CANDIDATES)
    )


def github_request(token: str, method: str, url: str, **kwargs) -> requests.Response:
    headers = kwargs.pop("headers", {})
    headers.setdefault("Authorization", f"token {token}")
    headers.setdefault("Accept", "application/vnd.github+json")
    headers.setdefault("User-Agent", "repo-convergence-v2")
    resp = requests.request(method, url, headers=headers, timeout=60, **kwargs)
    if resp.status_code == 401:
        raise SystemExit("GitHub API returned 401. Check token validity and scopes.")
    return resp


def get_github_user(token: str, user_override: str = "") -> str:
    if user_override:
        return user_override
    resp = github_request(token, "GET", f"{GITHUB_API}/user")
    data = resp.json()
    login = data.get("login")
    if not login:
        raise SystemExit("Unable to determine GitHub user from /user API.")
    return str(login)


def paginated_get(token: str, url: str, params: Optional[Dict[str, str]] = None) -> List[dict]:
    items: List[dict] = []
    page = 1
    params = dict(params or {})
    per_page = 100
    while True:
        params["page"] = str(page)
        params["per_page"] = str(per_page)
        resp = github_request(token, "GET", url, params=params)
        batch = resp.json()
        if not isinstance(batch, list):
            break
        items.extend(batch)
        if len(batch) < per_page:
            break
        page += 1
    return items


def parse_github_remote(url: str) -> Tuple[Optional[str], Optional[str]]:
    if not url:
        return None, None
    url = url.strip()
    if url.startswith("git@"):
        m = re.search(r"git@github\.com:([^/]+)/(.+?)(?:\.git)?$", url, flags=re.IGNORECASE)
        if m:
            return m.group(1), m.group(2)
        return None, None
    if "github.com" not in url.lower():
        return None, None
    m = re.search(r"github\.com/([^/]+)/(.+?)(?:\.git)?$", url, flags=re.IGNORECASE)
    if m:
        return m.group(1), m.group(2)
    return None, None


def get_repo_head(path: Path) -> Optional[str]:
    try:
        result = run_git(path, "rev-parse", "HEAD")
    except Exception:
        return None
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def get_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def get_file_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def files_identical(a: Path, b: Path) -> bool:
    if not a.exists() or not b.exists():
        return False
    try:
        return filecmp.cmp(a, b, shallow=False)
    except OSError:
        return False


def path_identity_key(owner: str, repo_kebab: str) -> str:
    return f"github:{owner.lower()}/{repo_kebab}"


def local_unbound_key(kebab: str) -> str:
    return f"local-unbound:{kebab}"


def normalise_text(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


# ---------------------------------------------------------------------------
# DATA STRUCTURES
# ---------------------------------------------------------------------------


@dataclass
class LocalRepo:
    base_label: str
    path: Path
    folder_name: str
    kebab_name: str
    origin_url: Optional[str]
    origin_owner: Optional[str]
    origin_repo: Optional[str]
    head: Optional[str]
    mtime: float
    logical_key: Optional[str] = None


@dataclass
class RemoteRepo:
    owner: str
    name: str
    kebab_name: str
    clone_url: str
    default_branch: str
    private: bool

    @property
    def logical_key(self) -> str:
        return path_identity_key(self.owner, self.kebab_name)

    @property
    def html_url(self) -> str:
        return f"https://github.com/{self.owner}/{self.name}"

    @property
    def canonical_clone_url(self) -> str:
        return f"https://github.com/{self.owner}/{self.kebab_name}.git"


@dataclass
class CanonicalEntry:
    logical_key: str
    kebab_name: str
    remote: Optional[RemoteRepo] = None
    locals_D: List[LocalRepo] = field(default_factory=list)
    locals_Z: List[LocalRepo] = field(default_factory=list)

    def locals_for_base(self, base_label: str) -> List[LocalRepo]:
        return self.locals_D if base_label == "D" else self.locals_Z


@dataclass
class Action:
    kind: str
    description: str
    details: Dict[str, str]


# ---------------------------------------------------------------------------
# SCANNING
# ---------------------------------------------------------------------------


def scan_local_base(base: Path, base_label: str) -> List[LocalRepo]:
    repos: List[LocalRepo] = []
    if not base.is_dir():
        return repos
    for entry in sorted(base.iterdir(), key=lambda p: p.name.lower()):
        if not entry.is_dir():
            continue
        if entry.name == ARCHIVE_DIR_NAME:
            continue
        if not is_git_repo(entry):
            continue
        origin_url = None
        origin_owner = None
        origin_repo = None
        try:
            result = run_git(entry, "remote", "get-url", "origin")
            if result.returncode == 0:
                origin_url = result.stdout.strip() or None
                origin_owner, origin_repo = parse_github_remote(origin_url or "")
        except Exception:
            origin_url = None
        repos.append(
            LocalRepo(
                base_label=base_label,
                path=entry,
                folder_name=entry.name,
                kebab_name=to_kebab(entry.name),
                origin_url=origin_url,
                origin_owner=origin_owner,
                origin_repo=origin_repo,
                head=get_repo_head(entry),
                mtime=get_mtime(entry),
            )
        )
    return repos


def scan_github_repos(token: str) -> Tuple[Dict[str, RemoteRepo], Dict[str, List[RemoteRepo]]]:
    remotes_by_key: Dict[str, RemoteRepo] = {}
    remotes_by_kebab: Dict[str, List[RemoteRepo]] = {}

    def add_remote(rec: dict) -> None:
        owner = str(rec["owner"]["login"])
        name = str(rec["name"])
        kebab = to_kebab(name)
        remote = RemoteRepo(
            owner=owner,
            name=name,
            kebab_name=kebab,
            clone_url=rec.get("clone_url") or f"https://github.com/{owner}/{name}.git",
            default_branch=rec.get("default_branch") or "main",
            private=bool(rec.get("private")),
        )
        remotes_by_key[remote.logical_key] = remote
        remotes_by_kebab.setdefault(kebab, []).append(remote)

    for rec in paginated_get(token, f"{GITHUB_API}/user/repos", params={"affiliation": "owner"}):
        add_remote(rec)

    for org in paginated_get(token, f"{GITHUB_API}/user/orgs"):
        org_login = org.get("login")
        if not org_login:
            continue
        for rec in paginated_get(token, f"{GITHUB_API}/orgs/{org_login}/repos"):
            add_remote(rec)

    return remotes_by_key, remotes_by_kebab


# ---------------------------------------------------------------------------
# CANONICAL GROUPING
# ---------------------------------------------------------------------------


def assign_logical_key(local_repo: LocalRepo, remotes_by_kebab: Dict[str, List[RemoteRepo]]) -> str:
    if local_repo.origin_owner and local_repo.origin_repo:
        return path_identity_key(local_repo.origin_owner, to_kebab(local_repo.origin_repo))
    candidates = remotes_by_kebab.get(local_repo.kebab_name, [])
    if len(candidates) == 1:
        return candidates[0].logical_key
    return local_unbound_key(local_repo.kebab_name)


def build_canonical_map(
    locals_D: List[LocalRepo],
    locals_Z: List[LocalRepo],
    remotes_by_key: Dict[str, RemoteRepo],
    remotes_by_kebab: Dict[str, List[RemoteRepo]],
) -> Dict[str, CanonicalEntry]:
    canonical: Dict[str, CanonicalEntry] = {}

    for key, remote in remotes_by_key.items():
        canonical[key] = CanonicalEntry(logical_key=key, kebab_name=remote.kebab_name, remote=remote)

    def ensure_for_local(local_repo: LocalRepo) -> CanonicalEntry:
        logical_key = assign_logical_key(local_repo, remotes_by_kebab)
        local_repo.logical_key = logical_key
        if logical_key not in canonical:
            canonical[logical_key] = CanonicalEntry(
                logical_key=logical_key,
                kebab_name=local_repo.kebab_name,
                remote=None,
            )
        return canonical[logical_key]

    for repo in locals_D:
        ensure_for_local(repo).locals_D.append(repo)

    for repo in locals_Z:
        ensure_for_local(repo).locals_Z.append(repo)

    return canonical


# ---------------------------------------------------------------------------
# README MANAGEMENT
# ---------------------------------------------------------------------------


def choose_workflow_filename(repo_dir: Path) -> Optional[str]:
    wf_dir = repo_dir / ".github" / "workflows"
    if not wf_dir.is_dir():
        return None
    candidates = sorted(
        [p.name for p in wf_dir.iterdir() if p.is_file() and p.suffix.lower() in {".yml", ".yaml"}],
        key=str.lower,
    )
    return candidates[0] if candidates else None


def extract_preserved_language(existing: str) -> str:
    match = README_LANGUAGE_META_RE.search(existing)
    if match:
        value = match.group(1).strip()
        if value:
            return value
    return README_DEFAULT_LANGUAGE


def strip_leading_h1_for_repo(body: str, repo_name: str) -> str:
    body = body.lstrip("\ufeff\n")
    pattern = re.compile(rf"^#\s+{re.escape(repo_name)}\s*(?:\n+|$)", re.IGNORECASE)
    return pattern.sub("", body, count=1).lstrip("\n")


def strip_known_auto_header(existing: str, repo_name: str) -> str:
    text = normalise_text(existing).lstrip("\ufeff")

    managed_re = re.compile(
        re.escape(README_HEADER_START) + r".*?" + re.escape(README_HEADER_END) + r"\s*",
        flags=re.DOTALL | re.IGNORECASE,
    )
    text = managed_re.sub("", text, count=1)

    if text.lstrip().startswith(README_OLD_MARKER):
        lines = text.split("\n")
        if lines and lines[0].strip() == README_OLD_MARKER:
            lines = lines[1:]
        text = "\n".join(lines).lstrip("\n")

        while True:
            p_block = re.match(r"^<p\s+align=\"center\">.*?</p>\s*", text, flags=re.DOTALL | re.IGNORECASE)
            if not p_block:
                break
            text = text[p_block.end():]

        badge_lines: List[str] = []
        body_lines = text.split("\n")
        idx = 0
        while idx < len(body_lines):
            line = body_lines[idx].strip()
            if not line:
                badge_lines.append(body_lines[idx])
                idx += 1
                continue
            looks_like_badge = (
                "shields.io" in line
                or line.startswith("![")
                or (line.startswith("[") and "](https://github.com/" in line)
            )
            if looks_like_badge:
                badge_lines.append(body_lines[idx])
                idx += 1
                continue
            break
        text = "\n".join(body_lines[idx:]).lstrip("\n")

        if text.lower().startswith("<p align=\"center\">") and "/assets/" in text[:1000].lower():
            p_block = re.match(r"^<p\s+align=\"center\">.*?</p>\s*", text, flags=re.DOTALL | re.IGNORECASE)
            if p_block:
                text = text[p_block.end():]

    text = strip_leading_h1_for_repo(text, repo_name)
    return text.lstrip("\n")


def build_readme_header(owner: str, repo: str, repo_dir: Path, language: str) -> str:
    workflow = choose_workflow_filename(repo_dir)
    encoded_language = quote(language, safe="") if language else quote(README_DEFAULT_LANGUAGE, safe="")

    if workflow:
        build_badge = (
            f'  <a href="https://github.com/{owner}/{repo}/actions/workflows/{workflow}">\n'
            f'    <img src="https://img.shields.io/github/actions/workflow/status/{owner}/{repo}/{workflow}?label=build" alt="Build Status">\n'
            f'  </a>'
        )
    else:
        build_badge = (
            f'  <a href="https://github.com/{owner}/{repo}/actions">\n'
            f'    <img src="https://img.shields.io/badge/build-workflow%20not%20set-lightgrey.svg" alt="Build Status">\n'
            f'  </a>'
        )

    return (
        f"{README_HEADER_START}\n"
        f"<!-- repo-convergence:language={language or README_DEFAULT_LANGUAGE} -->\n"
        f"# {repo}\n\n"
        f"<p align=\"center\">\n"
        f"  <a href=\"https://github.com/{owner}/{repo}/releases\">\n"
        f"    <img src=\"https://img.shields.io/github/v/release/{owner}/{repo}?color=4CAF50&label=release\" alt=\"Release Version\">\n"
        f"  </a>\n"
        f"  <a href=\"https://github.com/{owner}/{repo}/blob/main/LICENSE\">\n"
        f"    <img src=\"https://img.shields.io/badge/license-Hybrid-green.svg\" alt=\"License\">\n"
        f"  </a>\n"
        f"  <img src=\"https://img.shields.io/badge/platform-Windows%20%7C%20Linux%20%7C%20macOS-lightgrey.svg\" alt=\"Platform\">\n"
        f"{build_badge}\n"
        f"</p>\n\n"
        f"[![GitHub](https://img.shields.io/badge/GitHub-{owner}%2F{repo}-blue?logo=github&style=flat-square)](https://github.com/{owner}/{repo})\n"
        f"![Commits](https://img.shields.io/github/commit-activity/m/{owner}/{repo}?label=commits)\n"
        f"![Last Commit](https://img.shields.io/github/last-commit/{owner}/{repo})\n"
        f"![Issues](https://img.shields.io/github/issues/{owner}/{repo})\n"
        f"[![Version](https://img.shields.io/badge/version-3.1.1-blue.svg)](https://github.com/{owner}/{repo})\n"
        f"[![Platform](https://img.shields.io/badge/platform-Windows%2010%2F11-lightgrey.svg)](https://www.microsoft.com/windows)\n"
        f"[![Language](https://img.shields.io/badge/language-{encoded_language}-blue.svg)](#)\n"
        f"[![Status](https://img.shields.io/badge/Status-Alpha%20Release-orange?style=flat-square)](#)\n\n"
        f"<p align=\"center\">\n"
        f"  <img src=\"/assets/{repo}-banner.png\" width=\"850\" alt=\"{repo} Banner\">\n"
        f"</p>\n"
        f"{README_HEADER_END}\n"
    )


def build_default_readme_body(repo: str) -> str:
    return (
        "## Overview\n\n"
        f"Describe what **{repo}** does, why it exists, and what problem it solves.\n\n"
        "## Status\n\n"
        "Document the current maturity, stability, and short roadmap.\n\n"
        "## Getting Started\n\n"
        "Add install, build, and usage instructions here.\n"
    )


def update_readme(repo_dir: Path, owner: str, repo: str, apply_changes: bool) -> Tuple[bool, str]:
    readme_path = repo_dir / "README.md"
    existing = ""
    if readme_path.is_file():
        try:
            existing = readme_path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            existing = ""

    language = extract_preserved_language(existing)
    stripped_body = strip_known_auto_header(existing, repo)
    header = build_readme_header(owner, repo, repo_dir, language)

    if stripped_body.strip():
        new_content = header.rstrip() + "\n\n" + stripped_body.lstrip()
    else:
        new_content = header.rstrip() + "\n\n" + build_default_readme_body(repo)

    changed = normalise_text(existing).strip() != normalise_text(new_content).strip()
    if changed and apply_changes:
        readme_path.write_text(new_content, encoding="utf-8", newline="\n")
    return changed, str(readme_path)


# ---------------------------------------------------------------------------
# DOCUMENT TEMPLATE MANAGEMENT
# ---------------------------------------------------------------------------


def ensure_core_documents(repo_dir: Path, docs_base: Path, apply_changes: bool) -> List[str]:
    created: List[str] = []
    for filename in DOC_TEMPLATE_FILES:
        src = docs_base / filename
        dst = repo_dir / filename
        if dst.exists():
            continue
        if not src.is_file():
            continue
        if apply_changes:
            shutil.copy2(src, dst)
        created.append(filename)
    return created


# ---------------------------------------------------------------------------
# PLANNING
# ---------------------------------------------------------------------------


def choose_target_repo(repos: List[LocalRepo], target_path: Path, remote: Optional[RemoteRepo]) -> LocalRepo:
    exact = [r for r in repos if r.path == target_path]
    if exact:
        if remote:
            exact_remote = [
                r for r in exact
                if (r.origin_owner or "").lower() == remote.owner.lower()
                and to_kebab(r.origin_repo or r.folder_name) == remote.kebab_name
            ]
            if exact_remote:
                return max(exact_remote, key=lambda r: r.mtime)
        return max(exact, key=lambda r: r.mtime)

    if remote:
        with_origin = [
            r for r in repos
            if (r.origin_owner or "").lower() == remote.owner.lower()
            and to_kebab(r.origin_repo or r.folder_name) == remote.kebab_name
        ]
        if with_origin:
            return max(with_origin, key=lambda r: r.mtime)

    already_kebab = [r for r in repos if r.folder_name == target_path.name]
    if already_kebab:
        return max(already_kebab, key=lambda r: r.mtime)

    return max(repos, key=lambda r: r.mtime)


def preferred_source_repo(entry: CanonicalEntry, exclude_base: str) -> Optional[LocalRepo]:
    candidates = entry.locals_Z if exclude_base == "D" else entry.locals_D
    if not candidates:
        return None
    return max(candidates, key=lambda r: r.mtime)


def build_existing_path_map(canonical: Dict[str, CanonicalEntry]) -> Dict[Tuple[str, str], str]:
    path_map: Dict[Tuple[str, str], str] = {}
    for key, entry in canonical.items():
        for repo in entry.locals_D + entry.locals_Z:
            path_map[(repo.base_label, str(repo.path))] = key
    return path_map


def plan_actions(
    canonical: Dict[str, CanonicalEntry],
    code_base: Path,
    nas_base: Path,
    docs_base: Path,
    rename_github: bool,
) -> List[Action]:
    actions: List[Action] = []
    existing_paths = build_existing_path_map(canonical)

    def plan_base(entry: CanonicalEntry, base_label: str, base_path: Path) -> None:
        repos = entry.locals_for_base(base_label)
        target_path = base_path / entry.kebab_name
        occupied_by_other = target_path.exists() and existing_paths.get((base_label, str(target_path))) not in {None, entry.logical_key}

        if occupied_by_other:
            actions.append(
                Action(
                    kind="warn",
                    description=f"Target path {target_path} is occupied by a different logical repo; skipping automatic convergence",
                    details={"base": base_label, "path": str(target_path), "logical_key": entry.logical_key},
                )
            )
            return

        if repos:
            target_repo = choose_target_repo(repos, target_path, entry.remote)
            if target_repo.path != target_path:
                if target_path.exists():
                    actions.append(
                        Action(
                            kind="merge_repo",
                            description=f"Merge {target_repo.path.name} into canonical {target_path.name} under {base_path}",
                            details={
                                "base": base_label,
                                "src": str(target_repo.path),
                                "dst": str(target_path),
                            },
                        )
                    )
                else:
                    actions.append(
                        Action(
                            kind="rename_repo",
                            description=f"Rename {target_repo.path.name} -> {target_path.name} under {base_path}",
                            details={
                                "base": base_label,
                                "src": str(target_repo.path),
                                "dst": str(target_path),
                            },
                        )
                    )

            for repo in repos:
                if repo.path == target_repo.path:
                    continue
                actions.append(
                    Action(
                        kind="merge_repo",
                        description=f"Merge duplicate {repo.path.name} into canonical {target_path.name} under {base_path}",
                        details={
                            "base": base_label,
                            "src": str(repo.path),
                            "dst": str(target_path),
                        },
                    )
                )

            active_target = target_path
        else:
            active_target = target_path
            if entry.remote:
                actions.append(
                    Action(
                        kind="clone",
                        description=f"Clone {entry.remote.owner}/{entry.remote.kebab_name} into {target_path}",
                        details={
                            "base": base_label,
                            "clone_url": entry.remote.canonical_clone_url,
                            "dest": str(target_path),
                        },
                    )
                )
            else:
                source = preferred_source_repo(entry, exclude_base=base_label)
                if source is not None:
                    actions.append(
                        Action(
                            kind="copy_repo",
                            description=f"Copy {source.path} -> {target_path}",
                            details={
                                "base": base_label,
                                "src": str(source.path),
                                "dst": str(target_path),
                            },
                        )
                    )

        owner = entry.remote.owner if entry.remote else (entry.kebab_name if False else "")
        if entry.remote:
            actions.append(
                Action(
                    kind="set_origin",
                    description=f"Ensure origin URL for {active_target} points to {entry.remote.owner}/{entry.remote.kebab_name}",
                    details={
                        "base": base_label,
                        "repo_dir": str(active_target),
                        "clone_url": entry.remote.canonical_clone_url,
                    },
                )
            )
            actions.append(
                Action(
                    kind="readme",
                    description=f"Ensure managed README header for {active_target}",
                    details={
                        "base": base_label,
                        "repo_dir": str(active_target),
                        "owner": entry.remote.owner,
                        "repo": entry.kebab_name,
                    },
                )
            )
        else:
            actions.append(
                Action(
                    kind="readme",
                    description=f"Ensure managed README header for {active_target}",
                    details={
                        "base": base_label,
                        "repo_dir": str(active_target),
                        "owner": "whisprer",
                        "repo": entry.kebab_name,
                    },
                )
            )

        actions.append(
            Action(
                kind="core_docs",
                description=f"Ensure core docs exist in {active_target}",
                details={
                    "base": base_label,
                    "repo_dir": str(active_target),
                    "docs_base": str(docs_base),
                },
            )
        )

    for entry in sorted(canonical.values(), key=lambda e: e.logical_key):
        if entry.remote and rename_github and entry.remote.name != entry.kebab_name:
            actions.append(
                Action(
                    kind="rename_github_repo",
                    description=f"Rename GitHub repo {entry.remote.owner}/{entry.remote.name} -> {entry.kebab_name}",
                    details={
                        "owner": entry.remote.owner,
                        "old_name": entry.remote.name,
                        "new_name": entry.kebab_name,
                    },
                )
            )
        plan_base(entry, "D", code_base)
        plan_base(entry, "Z", nas_base)

    ordered_kinds = [
        "warn",
        "rename_github_repo",
        "rename_repo",
        "merge_repo",
        "clone",
        "copy_repo",
        "set_origin",
        "core_docs",
        "readme",
    ]
    order_index = {kind: idx for idx, kind in enumerate(ordered_kinds)}
    actions.sort(key=lambda a: (order_index.get(a.kind, 999), a.description.lower()))
    return actions


# ---------------------------------------------------------------------------
# ACTION APPLICATION
# ---------------------------------------------------------------------------


def archive_root_for(base_path: Path) -> Path:
    return base_path / ARCHIVE_DIR_NAME


def unique_archive_path(base_path: Path, repo_name: str, suffix: str) -> Path:
    root = archive_root_for(base_path)
    stamp = utc_stamp()
    candidate = root / f"{stamp}__{repo_name}__{suffix}"
    idx = 1
    while candidate.exists():
        idx += 1
        candidate = root / f"{stamp}__{repo_name}__{suffix}__{idx}"
    return candidate


def merge_repo_dirs(src: Path, dst: Path, base_path: Path, apply_changes: bool) -> Dict[str, int]:
    stats = {
        "copied_new": 0,
        "overwrote_newer": 0,
        "skipped_older": 0,
        "identical": 0,
    }
    overwrite_backup_root = unique_archive_path(base_path, src.name, "overwritten-target-files")
    src_archive_path = unique_archive_path(base_path, src.name, "merged-source-repo")

    if not src.exists():
        return stats

    if not dst.exists():
        if apply_changes:
            safe_case_rename(src, dst)
        return stats

    for root, dirnames, filenames in os.walk(src):
        root_path = Path(root)
        rel_root = root_path.relative_to(src)
        if rel_root.parts and rel_root.parts[0] == ".git":
            dirnames[:] = []
            continue
        if rel_root.parts and rel_root.parts[0] == ARCHIVE_DIR_NAME:
            dirnames[:] = []
            continue

        for dirname in list(dirnames):
            if dirname == ".git" or dirname == ARCHIVE_DIR_NAME:
                dirnames.remove(dirname)

        target_dir = dst / rel_root
        if apply_changes:
            target_dir.mkdir(parents=True, exist_ok=True)

        for filename in filenames:
            src_file = root_path / filename
            rel_file = src_file.relative_to(src)
            if rel_file.parts and rel_file.parts[0] == ".git":
                continue
            dst_file = dst / rel_file

            if not dst_file.exists():
                stats["copied_new"] += 1
                if apply_changes:
                    dst_file.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src_file, dst_file)
                continue

            if files_identical(src_file, dst_file):
                stats["identical"] += 1
                continue

            src_mtime = get_file_mtime(src_file)
            dst_mtime = get_file_mtime(dst_file)
            if src_mtime > dst_mtime:
                stats["overwrote_newer"] += 1
                if apply_changes:
                    backup_file = overwrite_backup_root / rel_file
                    backup_file.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(dst_file, backup_file)
                    shutil.copy2(src_file, dst_file)
            else:
                stats["skipped_older"] += 1

    if apply_changes:
        ensure_dir(src_archive_path.parent, True)
        safe_case_rename(src, src_archive_path)

    return stats


def set_origin_url(repo_dir: Path, clone_url: str, apply_changes: bool) -> Tuple[bool, str]:
    if not repo_dir.exists() or not is_git_repo(repo_dir):
        return False, "repo missing or not a git repo"
    result = run_git(repo_dir, "remote", "get-url", "origin")
    current = result.stdout.strip() if result.returncode == 0 else ""
    if current == clone_url:
        return False, "origin already correct"
    if apply_changes:
        if result.returncode == 0:
            cmd = run_git(repo_dir, "remote", "set-url", "origin", clone_url)
        else:
            cmd = run_git(repo_dir, "remote", "add", "origin", clone_url)
        if cmd.returncode != 0:
            return False, cmd.stderr.strip() or "git remote update failed"
    return True, current or "<missing>"


def rename_github_repo(token: str, owner: str, old_name: str, new_name: str, apply_changes: bool) -> Tuple[bool, str]:
    if old_name == new_name:
        return False, "already canonical"
    if not apply_changes:
        return True, "planned"
    resp = github_request(
        token,
        "PATCH",
        f"{GITHUB_API}/repos/{owner}/{old_name}",
        json={"name": new_name},
    )
    if resp.status_code not in {200, 201}:
        try:
            detail = resp.json()
        except Exception:
            detail = resp.text
        return False, f"GitHub rename failed: {detail}"
    return True, "renamed"


def serialise_actions(actions: List[Action]) -> List[dict]:
    return [{"kind": a.kind, "description": a.description, "details": a.details} for a in actions]


def apply_actions(actions: List[Action], token: str, apply_changes: bool) -> None:
    plan_path = Path(PLAN_FILENAME)
    plan_path.write_text(json.dumps(serialise_actions(actions), indent=2), encoding="utf-8")
    log(f"Planned {len(actions)} actions. Plan written to {plan_path}")

    for act in actions:
        log(f"- {act.kind.upper()}: {act.description}")
        if act.kind == "warn":
            continue
        if not apply_changes:
            continue

        if act.kind == "rename_github_repo":
            ok, detail = rename_github_repo(
                token,
                act.details["owner"],
                act.details["old_name"],
                act.details["new_name"],
                apply_changes=True,
            )
            if ok:
                log("  GitHub rename applied")
            else:
                log("  [ERR]", detail)

        elif act.kind == "rename_repo":
            src = Path(act.details["src"])
            dst = Path(act.details["dst"])
            if not src.exists():
                log("  [SKIP] source missing", src)
                continue
            if dst.exists():
                log("  [SKIP] destination already exists", dst)
                continue
            ensure_dir(dst.parent, True)
            safe_case_rename(src, dst)
            log("  renamed", src, "->", dst)

        elif act.kind == "merge_repo":
            src = Path(act.details["src"])
            dst = Path(act.details["dst"])
            base = dst.parent
            if not src.exists():
                log("  [SKIP] source missing", src)
                continue
            ensure_dir(dst.parent, True)
            stats = merge_repo_dirs(src, dst, base, apply_changes=True)
            log("  merge stats", json.dumps(stats, sort_keys=True))

        elif act.kind == "clone":
            dest = Path(act.details["dest"])
            if dest.exists():
                log("  [SKIP] destination already exists", dest)
                continue
            ensure_dir(dest.parent, True)
            result = run_git(None, "clone", act.details["clone_url"], str(dest))
            if result.returncode != 0:
                log("  [ERR] git clone failed:", result.stderr.strip())
            else:
                log("  cloned to", dest)

        elif act.kind == "copy_repo":
            src = Path(act.details["src"])
            dst = Path(act.details["dst"])
            if not src.exists():
                log("  [SKIP] source missing", src)
                continue
            if dst.exists():
                log("  [SKIP] destination already exists", dst)
                continue
            shutil.copytree(src, dst)
            log("  copied", src, "->", dst)

        elif act.kind == "set_origin":
            repo_dir = Path(act.details["repo_dir"])
            changed, detail = set_origin_url(repo_dir, act.details["clone_url"], apply_changes=True)
            if changed:
                log("  origin updated from", detail)
            else:
                log("  [INFO]", detail)

        elif act.kind == "core_docs":
            repo_dir = Path(act.details["repo_dir"])
            if not repo_dir.exists():
                log("  [SKIP] repo missing", repo_dir)
                continue
            created = ensure_core_documents(repo_dir, Path(act.details["docs_base"]), apply_changes=True)
            if created:
                log("  created", ", ".join(created))
            else:
                log("  no core doc changes")

        elif act.kind == "readme":
            repo_dir = Path(act.details["repo_dir"])
            if not repo_dir.exists():
                log("  [SKIP] repo missing", repo_dir)
                continue
            changed, readme_path = update_readme(
                repo_dir,
                act.details["owner"],
                act.details["repo"],
                apply_changes=True,
            )
            if changed:
                log("  updated", readme_path)
            else:
                log("  no README change")

    if not apply_changes:
        log("Dry-run mode: no changes applied.")
    else:
        log("All requested actions applied. Surplus merged repos were archived; nothing was deleted.")


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Converge and normalise Git repos across D:/code, Z:/GitHub-Repos, and GitHub."
    )
    parser.add_argument("--apply", action="store_true", help="Apply changes. Default is dry-run.")
    parser.add_argument("--code-base", default=str(DEFAULT_CODE_BASE), help="Local repo root. Default: D:/code")
    parser.add_argument("--nas-base", default=str(DEFAULT_NAS_BASE), help="NAS repo root. Default: Z:/GitHub-Repos")
    parser.add_argument(
        "--docs-base",
        default=str(DEFAULT_DOCS_BASE),
        help="Directory containing template docs such as CHANGELOG.md and LICENSE.md.",
    )
    parser.add_argument(
        "--github-user",
        default=os.environ.get("GITHUB_USER", "").strip(),
        help="Optional GitHub user override. Default uses /user API or GITHUB_USER env var.",
    )
    parser.add_argument(
        "--no-rename-github",
        action="store_true",
        help="Do not plan/apply GitHub repo renames even if remote repo names are not kebab-case.",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)

    code_base = Path(args.code_base)
    nas_base = Path(args.nas_base)
    docs_base = Path(args.docs_base)
    rename_github = not args.no_rename_github

    token = get_token()
    github_user = get_github_user(token, user_override=args.github_user)

    log("Git executable:", GIT_EXE)
    log("GitHub user:", github_user)
    log("Code base:", code_base)
    log("NAS base:", nas_base)
    log("Docs base:", docs_base)

    log("Scanning local repositories")
    locals_D = scan_local_base(code_base, "D")
    locals_Z = scan_local_base(nas_base, "Z")
    log(f"  Found {len(locals_D)} repos under {code_base}")
    log(f"  Found {len(locals_Z)} repos under {nas_base}")

    log("Scanning GitHub repositories (user and orgs)")
    remotes_by_key, remotes_by_kebab = scan_github_repos(token)
    log(f"  Found {len(remotes_by_key)} GitHub repos across user + orgs")

    canonical = build_canonical_map(locals_D, locals_Z, remotes_by_key, remotes_by_kebab)
    log(f"Canonical logical entries: {len(canonical)}")

    actions = plan_actions(canonical, code_base, nas_base, docs_base, rename_github=rename_github)
    apply_actions(actions, token=token, apply_changes=args.apply)


if __name__ == "__main__":
    main()
