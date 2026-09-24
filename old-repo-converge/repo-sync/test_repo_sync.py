#!/usr/bin/env python3
"""
End-to-end self-test for repo_sync.py.

Builds a throwaway fake GitHub (a tiny local API server + bare repos), a fake
P52 folder and a fake NAS folder inside a temp directory, then runs the real
script against them through ~35 scenarios: dry run, apply, a second run that
must change nothing, the delete brakes, the --local-only/--stamps policies, a
change sneaking in between the plan and YES, and the dupes cleanup.

It never touches your real repos, your GitHub account or your git config:
git runs with GIT_CONFIG_GLOBAL pointing at a temp file that rewrites
https://github.com/ to the local bare repos.

Run:  py -3 test_repo_sync.py          (Windows)
      python3 test_repo_sync.py        (Linux)
Exit code 0 = all checks passed.
"""

from __future__ import annotations

import http.server
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import urllib.parse
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "repo_sync.py"
TOKEN = "test-token"
ME = "whisprer"
ORG = "orgA"

V2_START = "<!-- repo-convergence:readme-header:start -->"
V2_END = "<!-- repo-convergence:readme-header:end -->"
STAMP_DOCS = ["CHANGELOG.md", "CODE_OF_CONDUCT.md", "CONTRIBUTING.md", "LICENSE.md", "SECURITY.md"]

PASSED: list[str] = []
FAILED: list[str] = []


def check(cond: bool, msg: str) -> None:
    (PASSED if cond else FAILED).append(msg)
    print(("  PASS  " if cond else "  FAIL  ") + msg)


# ---------------------------------------------------------------------------
# fake GitHub
# ---------------------------------------------------------------------------


class FakeGitHub:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.repos: dict[str, dict] = {}       # "owner/name" lower -> record
        self.moved: dict[str, str] = {}        # old key -> new key
        self.by_id: dict[int, str] = {}
        self.next_id = 1000
        self.orgs = [ORG]
        self.lock = threading.Lock()

    def bare(self, owner: str, name: str) -> Path:
        return self.root / owner / f"{name}.git"

    def add(self, owner: str, name: str, private: bool = False, archived: bool = False) -> Path:
        p = self.bare(owner, name)
        p.parent.mkdir(parents=True, exist_ok=True)
        run_git(None, "init", "--bare", "-q", "-b", "main", str(p))
        with self.lock:
            self.next_id += 1
            rec = {"id": self.next_id, "owner": owner, "name": name, "private": private, "archived": archived}
            self.repos[f"{owner}/{name}".lower()] = rec
            self.by_id[self.next_id] = f"{owner}/{name}".lower()
        return p

    def delete(self, owner: str, name: str) -> None:
        key = f"{owner}/{name}".lower()
        rec = self.repos.pop(key)
        self.by_id.pop(rec["id"], None)
        force_rmtree(self.bare(owner, name))

    def move(self, old_owner: str, old_name: str, new_owner: str, new_name: str) -> None:
        old_key = f"{old_owner}/{old_name}".lower()
        rec = self.repos.pop(old_key)
        src, dst = self.bare(old_owner, old_name), self.bare(new_owner, new_name)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        # GitHub keeps git working on the old URL; a symlink (or a mirror copy on Windows) does that here
        try:
            os.symlink(dst, src, target_is_directory=True)
        except OSError:
            run_git(None, "clone", "--mirror", "-q", str(dst), str(src))
        rec["owner"], rec["name"] = new_owner, new_name
        new_key = f"{new_owner}/{new_name}".lower()
        self.repos[new_key] = rec
        self.by_id[rec["id"]] = new_key
        self.moved[old_key] = new_key

    def default_branch(self, rec: dict) -> str:
        r = run_git(self.bare(rec["owner"], rec["name"]), "symbolic-ref", "HEAD", check=False)
        return r.stdout.strip().replace("refs/heads/", "") or "main"

    def js(self, rec: dict, base: str) -> dict:
        o, n = rec["owner"], rec["name"]
        return {"id": rec["id"], "name": n, "full_name": f"{o}/{n}", "owner": {"login": o},
                "private": rec["private"], "archived": rec["archived"], "disabled": False, "fork": False,
                "default_branch": self.default_branch(rec), "clone_url": f"https://github.com/{o}/{n}.git"}


def make_handler(gh: FakeGitHub):
    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a) -> None:  # quiet
            pass

        def _send(self, code: int, data=None, headers=None) -> None:
            body = b"" if data is None else json.dumps(data).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _ok(self) -> bool:
            if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                self._send(401, {"message": "Bad credentials"})
                return False
            return True

        def _page(self, items: list) -> None:
            u = urllib.parse.urlparse(self.path)
            q = urllib.parse.parse_qs(u.query)
            per = min(int(q.get("per_page", ["30"])[0]), 3)   # force pagination
            page = int(q.get("page", ["1"])[0])
            chunk = items[(page - 1) * per: page * per]
            headers = {}
            if page * per < len(items):
                q2 = dict(q)
                q2["page"] = [str(page + 1)]
                q2["per_page"] = [str(per)]
                nxt = f"http://{self.headers['Host']}{u.path}?{urllib.parse.urlencode(q2, doseq=True)}"
                headers["Link"] = f'<{nxt}>; rel="next"'
            self._send(200, chunk, headers)

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}")

        def do_GET(self) -> None:
            if not self._ok():
                return
            base = f"http://{self.headers['Host']}"
            path = urllib.parse.urlparse(self.path).path
            if path == "/user":
                return self._send(200, {"login": ME})
            if path == "/user/orgs":
                return self._page([{"login": o} for o in gh.orgs])
            if path == "/user/repos":
                return self._page([gh.js(r, base) for k, r in sorted(gh.repos.items()) if r["owner"] == ME])
            m = re.match(r"^/orgs/([^/]+)/repos$", path)
            if m:
                return self._page([gh.js(r, base) for k, r in sorted(gh.repos.items())
                                   if r["owner"].lower() == m.group(1).lower()])
            m = re.match(r"^/repos/([^/]+)/([^/]+)$", path)
            if m:
                key = f"{m.group(1)}/{m.group(2)}".lower()
                if key in gh.moved and key not in gh.repos:
                    rid = gh.repos[gh.moved[key]]["id"]
                    return self._send(301, {"message": "Moved Permanently"},
                                      {"Location": f"{base}/repositories/{rid}"})
                if key in gh.repos:
                    return self._send(200, gh.js(gh.repos[key], base))
                return self._send(404, {"message": "Not Found"})
            m = re.match(r"^/repositories/(\d+)$", path)
            if m and int(m.group(1)) in gh.by_id:
                return self._send(200, gh.js(gh.repos[gh.by_id[int(m.group(1))]], base))
            self._send(404, {"message": "Not Found"})

        def do_POST(self) -> None:
            if not self._ok():
                return
            path = urllib.parse.urlparse(self.path).path
            body = self._body()
            owner = ME if path == "/user/repos" else (re.match(r"^/orgs/([^/]+)/repos$", path) or [None, None])[1]
            if not owner:
                return self._send(404, {"message": "Not Found"})
            key = f"{owner}/{body['name']}".lower()
            if key in gh.repos:
                return self._send(422, {"message": "Repository creation failed.",
                                        "errors": [{"message": "name already exists on this account"}]})
            gh.add(owner, body["name"], private=bool(body.get("private")))
            self._send(201, gh.js(gh.repos[key], f"http://{self.headers['Host']}"))

        def do_PATCH(self) -> None:
            if not self._ok():
                return
            m = re.match(r"^/repos/([^/]+)/([^/]+)$", urllib.parse.urlparse(self.path).path)
            key = f"{m.group(1)}/{m.group(2)}".lower() if m else ""
            if key not in gh.repos:
                return self._send(404, {"message": "Not Found"})
            body = self._body()
            rec = gh.repos[key]
            if "default_branch" in body:
                run_git(gh.bare(rec["owner"], rec["name"]), "symbolic-ref", "HEAD",
                        f"refs/heads/{body['default_branch']}")
            self._send(200, gh.js(rec, f"http://{self.headers['Host']}"))

    return H


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

ENV: dict[str, str] = {}


def run_git(cwd, *args, check=True, env=None):
    # the harness itself may look inside repos owned by another user (the 'tango' case)
    r = subprocess.run(["git", "-c", "safe.directory=*", *args], cwd=None if cwd is None else str(cwd), env=env or ENV,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed in {cwd}: {r.stderr}")
    return r


def force_rmtree(p: Path) -> None:
    def onerr(func, path, _):
        os.chmod(path, stat.S_IWRITE)
        func(path)
    if p.is_symlink():
        p.unlink()
    elif p.exists():
        if sys.version_info >= (3, 12):
            shutil.rmtree(p, onexc=onerr)
        else:
            shutil.rmtree(p, onerror=onerr)


def sha(repo: Path, ref: str = "HEAD") -> str:
    return run_git(repo, "rev-parse", ref).stdout.strip()


def bare_sha(gh: FakeGitHub, owner: str, name: str, ref: str = "refs/heads/main") -> str:
    r = run_git(gh.bare(owner, name), "rev-parse", "--verify", "-q", ref, check=False)
    return r.stdout.strip()


def commit(repo: Path, rel: str, content: str, msg: str) -> str:
    f = repo / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(content, encoding="utf-8")
    run_git(repo, "add", "--", rel)
    run_git(repo, "commit", "-q", "-m", msg)
    return sha(repo)


def seed(gh: FakeGitHub, owner: str, name: str, work: Path, files=None, private=False, archived=False) -> Path:
    """Create an online repo with one commit; returns a scratch clone."""
    gh.add(owner, name, private=private, archived=archived)
    w = work / f"seed-{owner}-{name}"
    run_git(None, "clone", "-q", f"https://github.com/{owner}/{name}.git", str(w))
    for rel, content in (files or {"README.md": f"# {name}\n\nhello\n"}).items():
        (w / rel).parent.mkdir(parents=True, exist_ok=True)
        (w / rel).write_text(content, encoding="utf-8")
    run_git(w, "add", "-A")
    run_git(w, "commit", "-q", "-m", "first")
    run_git(w, "push", "-q", "origin", "HEAD:refs/heads/main")
    return w


def clone_to(owner: str, name: str, dest: Path) -> Path:
    run_git(None, "clone", "-q", f"https://github.com/{owner}/{name}.git", str(dest))
    return dest


def push_online(work: Path, rel: str, content: str, msg: str) -> str:
    run_git(work, "pull", "-q", "--ff-only", "origin", "main")
    s = commit(work, rel, content, msg)
    run_git(work, "push", "-q", "origin", "HEAD:refs/heads/main")
    return s


def stamp_readme(repo: Path, name: str, docs: Path) -> None:
    """What the old repo_convergence.py did: managed header + template docs, uncommitted."""
    readme = repo / "README.md"
    existing = readme.read_text(encoding="utf-8") if readme.exists() else ""
    body = re.sub(rf"^#\s+{re.escape(name)}\s*\n+", "", existing, count=1, flags=re.I)
    header = (f"{V2_START}\n<!-- repo-convergence:language=FILL_ME -->\n# {name}\n\n"
              f"<p align=\"center\">\n  <img src=\"https://img.shields.io/badge/x-y-blue.svg\">\n</p>\n{V2_END}\n")
    readme.write_text(header.rstrip() + "\n\n" + body.lstrip(), encoding="utf-8")
    for d in STAMP_DOCS:
        if not (repo / d).exists():
            shutil.copy2(docs / d, repo / d)


class World:
    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.gh_root = tmp / "gh"
        self.p52 = tmp / "P52"
        self.nas = tmp / "NAS"
        self.bk = tmp / "backups"
        self.docs = tmp / "docs"
        self.state = tmp / "state"
        self.work = tmp / "work"
        self.other_root = tmp / "otherhost"
        for d in (self.gh_root, self.p52, self.nas, self.docs, self.work, self.other_root):
            d.mkdir(parents=True)
        for d in STAMP_DOCS:
            (self.docs / d).write_text(f"# {d}\n\nTemplate text for {d}.\n", encoding="utf-8")
        self.gh = FakeGitHub(self.gh_root)
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.gh))
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        gitconfig = tmp / "gitconfig"
        gitconfig.write_text(
            "[user]\n\tname = Test\n\temail = test@example.com\n"
            "[init]\n\tdefaultBranch = main\n"
            f"[url \"{self.gh_root.as_uri()}/\"]\n\tinsteadOf = https://github.com/\n\tinsteadOf = git@github.com:\n"
            f"[url \"{self.other_root.as_uri()}/\"]\n\tinsteadOf = https://git.example.org/\n"
            "[protocol \"file\"]\n\tallow = always\n"
            "[core]\n\tautocrlf = false\n", encoding="utf-8")
        ENV.clear()
        ENV.update(os.environ)
        for k in ("GITHUB_PAT", "GH_RENAME_PAT", "GITHUB_TOKEN", "GH_TOKEN", "GIT_DIR", "GIT_WORK_TREE"):
            ENV.pop(k, None)
        ENV.update({"GIT_CONFIG_GLOBAL": str(gitconfig), "GIT_CONFIG_NOSYSTEM": "1",
                    "REPO_SYNC_API_URL": f"http://127.0.0.1:{self.httpd.server_address[1]}",
                    "GITHUB_PAT": TOKEN, "GIT_TERMINAL_PROMPT": "0", "PYTHONIOENCODING": "utf-8"})

    def run(self, *extra: str, dry: bool = False, expect_rc=(0,)) -> tuple[str, dict, dict]:
        jr = self.tmp / "report.json"
        for f in (jr, Path(str(jr) + ".plan.json")):
            if f.exists():
                f.unlink()
        cmd = [sys.executable, str(SCRIPT), "sync", "--p52-base", str(self.p52), "--nas-base", str(self.nas),
               "--backup-root", str(self.bk), "--docs-base", str(self.docs), "--state-dir", str(self.state),
               "--jobs", "4", "--json-report", str(jr)]
        cmd += ["--dry-run"] if dry else ["--yes"]
        cmd += list(extra)
        p = subprocess.run(cmd, env=ENV, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                           encoding="utf-8", timeout=900)
        output = p.stdout
        if p.returncode not in expect_rc:
            print(output)
            raise SystemExit(f"repo_sync.py exited {p.returncode}")
        plan = json.loads(Path(str(jr) + ".plan.json").read_text(encoding="utf-8")) if Path(str(jr) + ".plan.json").exists() else {}
        rep = json.loads(jr.read_text(encoding="utf-8")) if jr.exists() else {}
        return output, plan, rep


def _run_interrupted(self, between, *extra: str) -> dict:
    """Start a real (non --yes) run, wait for the YES prompt, change something, then say YES."""
    jr = self.tmp / "gate.json"
    if jr.exists():
        jr.unlink()
    cmd = [sys.executable, str(SCRIPT), "sync", "--p52-base", str(self.p52), "--nas-base", str(self.nas),
           "--backup-root", str(self.bk), "--docs-base", str(self.docs), "--state-dir", str(self.state),
           "--jobs", "2", "--json-report", str(jr)] + list(extra)
    proc = subprocess.Popen(cmd, env=ENV, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8")
    seen = ""
    while "Type YES" not in seen:
        ch = proc.stdout.read(1)
        if not ch:
            break
        seen += ch
    between()
    rest, _ = proc.communicate("YES\n", timeout=300)
    if not jr.exists():
        print(seen + (rest or ""))
        raise SystemExit("gated run produced no report")
    return json.loads(jr.read_text(encoding="utf-8"))


World.run_interrupted = _run_interrupted  # type: ignore[attr-defined]


def reachable(repo: Path, commit_sha: str) -> bool:
    if not repo.exists():
        return False
    r = run_git(repo, "rev-list", "--all", check=False)
    return commit_sha in r.stdout.split()


def commit_in_backups(bk: Path, name_pat: str, commit_sha: str, scratch: Path) -> list[str]:
    """Backup zips (matching name_pat) whose bundle or .git holds commit_sha."""
    hits = []
    for z in zips(bk, name_pat):
        d = Path(tempfile.mkdtemp(dir=scratch))
        with zipfile.ZipFile(z) as zf:
            zf.extractall(d)
        if (d / "extras.bundle").exists():
            b = d / "b.git"
            run_git(None, "init", "-q", "--bare", str(b))
            run_git(b, "fetch", "-q", str(d / "extras.bundle"), "refs/*:refs/r/*", check=False)
            if run_git(b, "cat-file", "-e", commit_sha, check=False).returncode == 0:
                hits.append(z.name)
        for g in d.glob("*/.git"):
            if run_git(g.parent, "cat-file", "-e", commit_sha, check=False).returncode == 0:
                hits.append(z.name)
    return hits


def steps_of(report: dict, display_sub: str) -> list[dict]:
    return [s for r in report.get("results", []) if display_sub in r["display"] for s in r["steps"]]


def issues_of(report: dict, display_sub: str) -> list[dict]:
    return [i for r in report.get("results", []) if display_sub in r["display"] for i in r["issues"]]


def all_steps(report: dict) -> list[dict]:
    return [s for r in report.get("results", []) for s in r["steps"]]


def zips(bk: Path, pattern: str) -> list[Path]:
    return sorted(p for p in bk.rglob("*.zip") if re.search(pattern, p.name))


def origin(repo: Path) -> str:
    return run_git(repo, "config", "--get", "remote.origin.url", check=False).stdout.strip()


def is_clean(repo: Path) -> bool:
    return run_git(repo, "status", "--porcelain").stdout.strip() == ""


# ---------------------------------------------------------------------------
# unit checks (pure functions)
# ---------------------------------------------------------------------------


def unit_checks() -> None:
    sys.path.insert(0, str(HERE))
    import repo_sync as rs  # noqa: E402

    print("\n== unit checks")
    u = rs.parse_remote_url("git@github.com:whisprer/Foo.git")
    check(u.kind == "github" and u.owner == "whisprer" and u.name == "Foo", "parse scp github url")
    check(u.with_repo("orgA", "foo") == "git@github.com:orgA/foo.git", "scp url retarget keeps style")
    u = rs.parse_remote_url("https://whisprer@github.com/whisprer/bar/")
    check(u.kind == "github" and u.name == "bar" and u.user == "whisprer", "parse https url with user")
    check(u.with_repo("orgA", "bar") == "https://whisprer@github.com/orgA/bar.git", "https retarget keeps user")
    u = rs.parse_remote_url("git@github-work:whisprer/baz.git")
    check(u.kind == "github" and u.host == "github-work", "ssh host alias counts as github")
    check(rs.parse_remote_url("D:\\code\\thing").kind == "local", "windows path origin is local")
    check(rs.parse_remote_url("https://gitlab.com/a/b.git").kind == "other", "gitlab is 'other'")
    check(rs.redact_url("https://user:ghp_secret@github.com/a/b") == "https://user:***@github.com/a/b", "redact password")
    check("ghp_" not in rs.redact_url("https://ghp_abcdefghijklmnopqrstuvwxyz0123456789@github.com/a/b"), "redact token-as-user")
    err = ("fatal: detected dubious ownership in repository at '//nas/share/repo'\nTo add an exception for this "
           "directory, call:\n\n\tgit config --global --add safe.directory '%(prefix)///nas/share/repo'\n")
    check(rs.parse_safe_directory_hint(err) == "%(prefix)///nas/share/repo", "safe.directory hint (UNC form)")
    err2 = ("fatal: detected dubious ownership in repository at '/tmp/sp ace/r'q'\nTo add an exception for this "
            "directory, call:\n\n\tgit config --global --add safe.directory '/tmp/sp ace/r'\\''q'\n")
    check(rs.parse_safe_directory_hint(err2) == "/tmp/sp ace/r'q", "safe.directory hint with quotes")
    check(rs.safe_folder_name("con") == "con-repo" and rs.safe_folder_name("a:b") == "a-b", "windows-safe folder names")
    check(rs.github_repo_name("my repo!") == "my-repo", "github repo name sanitised")
    head = "# demo\n\nSome text I wrote.\n"
    stamped = (f"{V2_START}\n<!-- repo-convergence:language=FILL_ME -->\n# demo\n\n<p align=\"center\">x</p>\n"
               f"{V2_END}\n\nSome text I wrote.\n")
    check(rs.readme_is_pure_stamp(stamped, head, ["demo"]), "pure stamp detected")
    check(not rs.readme_is_pure_stamp(stamped.replace("I wrote", "I CHANGED"), head, ["demo"]),
          "stamp + real edit is NOT pure")
    check(not rs.readme_is_pure_stamp(head + "more\n", head, ["demo"]), "no marker = not a stamp")


# ---------------------------------------------------------------------------
# the scenario
# ---------------------------------------------------------------------------


def build_world(w: World) -> dict:
    gh, work, P, N, docs = w.gh, w.work, w.p52, w.nas, w.docs
    s: dict = {}

    # alpha: already in agreement everywhere
    seed(gh, ME, "alpha", work)
    clone_to(ME, "alpha", P / "alpha")
    clone_to(ME, "alpha", N / "alpha")
    # bravo: online only -> clone both
    seed(gh, ME, "bravo", work)
    # charlie: online moved ahead -> fast-forward P52 + NAS
    wc = seed(gh, ME, "charlie", work)
    clone_to(ME, "charlie", P / "charlie")
    clone_to(ME, "charlie", N / "charlie")
    s["charlie"] = push_online(wc, "new.txt", "new\n", "online change")
    # delta: P52 ahead -> push, NAS then pulls it
    seed(gh, ME, "delta", work)
    clone_to(ME, "delta", P / "delta")
    clone_to(ME, "delta", N / "delta")
    s["delta"] = commit(P / "delta", "local.txt", "mine\n", "local work")
    # echo: diverged -> NEEDS YOU, nothing forced
    we = seed(gh, ME, "echo", work)
    clone_to(ME, "echo", P / "echo")
    clone_to(ME, "echo", N / "echo")
    s["echo_local"] = commit(P / "echo", "l.txt", "l\n", "local")
    s["echo_online"] = push_online(we, "o.txt", "o\n", "online")
    # foxtrot: dirty but the incoming change doesn't touch it -> ff + WIP backup
    wf = seed(gh, ME, "foxtrot", work)
    clone_to(ME, "foxtrot", P / "foxtrot")
    (P / "foxtrot" / "scratch.txt").write_text("uncommitted idea\n", encoding="utf-8")
    s["foxtrot"] = push_online(wf, "other.txt", "x\n", "online")
    # golf: dirty file that the incoming change also touches -> blocked, nothing touched
    wg = seed(gh, ME, "golf", work, files={"README.md": "# golf\n", "shared.txt": "v1\n"})
    clone_to(ME, "golf", P / "golf")
    (P / "golf" / "shared.txt").write_text("my local edit\n", encoding="utf-8")
    s["golf_head"] = sha(P / "golf")
    push_online(wg, "shared.txt", "v2 online\n", "online edit")
    # hotel: transferred whisprer/hotel -> orgA/hotel; local origins still say whisprer/hotel
    seed(gh, ME, "hotel", work)
    clone_to(ME, "hotel", P / "hotel")
    clone_to(ME, "hotel", N / "hotel")
    gh.move(ME, "hotel", ORG, "hotel")
    # india: deleted online -> full backup + delete (P52 and NAS)
    seed(gh, ME, "india", work)
    clone_to(ME, "india", P / "india")
    clone_to(ME, "india", N / "india")
    gh.delete(ME, "india")
    # juliet: never online; NAS has an old copytree of it (like the old script made)
    j = P / "juliet"
    run_git(None, "init", "-q", "-b", "main", str(j))
    commit(j, "a.txt", "a\n", "one")
    run_git(j, "branch", "side")
    shutil.copytree(j, N / "juliet")
    s["juliet"] = commit(j, "b.txt", "b\n", "two")
    # kilo: P52 folder with no origin that IS whisprer/kilo (by history)
    seed(gh, ME, "kilo", work)
    clone_to(ME, "kilo", P / "kilo")
    run_git(P / "kilo", "remote", "remove", "origin")
    # lima: someone else's repo -> pull only, mirrored to NAS
    wl = seed(gh, "someoneelse", "lima", work)
    clone_to("someoneelse", "lima", P / "lima")
    s["lima"] = push_online(wl, "up.txt", "u\n", "upstream change")
    # mike: two P52 folders for the same repo -> only 'mike' synced, duplicate reported
    wm = seed(gh, ME, "mike", work)
    clone_to(ME, "mike", P / "mike")
    clone_to(ME, "mike", P / "mike-copy")
    s["mike_copy"] = sha(P / "mike-copy")
    push_online(wm, "m.txt", "m\n", "online")
    # november: same name in two accounts -> november + november--orgA
    seed(gh, ME, "november", work)
    seed(gh, ORG, "november", work)
    # oscar: branches deleted online (one merged, one with unique work)
    wo = seed(gh, ME, "oscar", work)
    clone_to(ME, "oscar", P / "oscar")
    o = P / "oscar"
    run_git(o, "switch", "-q", "-c", "feat")
    run_git(o, "push", "-q", "-u", "origin", "feat")
    run_git(o, "switch", "-q", "-c", "feat2")
    commit(o, "f2.txt", "f2\n", "feat2 work")
    run_git(o, "push", "-q", "-u", "origin", "feat2")
    s["oscar_feat2"] = commit(o, "f2b.txt", "only here\n", "unpushed feat2 work")
    run_git(o, "switch", "-q", "main")
    run_git(wo, "push", "-q", "origin", "--delete", "feat", "feat2")
    # papa: brand-new local branch -> published
    seed(gh, ME, "papa", work)
    clone_to(ME, "papa", P / "papa")
    run_git(P / "papa", "switch", "-q", "-c", "newbranch")
    s["papa"] = commit(P / "papa", "n.txt", "n\n", "new branch work")
    run_git(P / "papa", "switch", "-q", "main")
    # quebec: only change is the old stamps (P52 and NAS) -> commit+push on P52, NAS reset w/o backup
    seed(gh, ME, "quebec", work, files={"README.md": "# quebec\n\nWhat quebec does.\n"})
    clone_to(ME, "quebec", P / "quebec")
    clone_to(ME, "quebec", N / "quebec")
    stamp_readme(P / "quebec", "quebec", docs)
    stamp_readme(N / "quebec", "quebec", docs)
    # romeo: NAS has an extra branch + a dirty file -> backup + reset
    seed(gh, ME, "romeo", work)
    clone_to(ME, "romeo", P / "romeo")
    clone_to(ME, "romeo", N / "romeo")
    run_git(N / "romeo", "switch", "-q", "-c", "nas-junk")
    commit(N / "romeo", "j.txt", "junk\n", "nas only")
    run_git(N / "romeo", "switch", "-q", "main")
    (N / "romeo" / "README.md").write_text("edited on the NAS\n", encoding="utf-8")
    # sierra: NAS folder named differently -> renamed to match P52
    seed(gh, ME, "sierra", work)
    clone_to(ME, "sierra", P / "sierra")
    clone_to(ME, "sierra", N / "Sierra-Old")
    # tango: NAS copy owned by another user -> 'dubious ownership' handled
    wt = seed(gh, ME, "tango", work)
    clone_to(ME, "tango", P / "tango")
    clone_to(ME, "tango", N / "tango")
    s["tango"] = push_online(wt, "t.txt", "t\n", "online")
    s["tango_chowned"] = False
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        try:
            import pwd
            uid = pwd.getpwnam("claude").pw_uid if "claude" in [p.pw_name for p in pwd.getpwall()] else 65534
            for root, dirs, files in os.walk(N / "tango"):
                for x in dirs + files:
                    os.lchown(os.path.join(root, x), uid, uid)
            os.lchown(N / "tango", uid, uid)
            s["tango_chowned"] = True
        except (KeyError, OSError, ImportError):
            pass
    # uniform: origin set to a repo that was never created -> created + pushed
    u = P / "uniform"
    run_git(None, "init", "-q", "-b", "main", str(u))
    commit(u, "u.txt", "u\n", "first")
    run_git(u, "remote", "add", "origin", f"https://github.com/{ME}/uniform.git")
    # victor: archived online, P52 ahead -> reported, not pushed
    seed(gh, ME, "victor", work, archived=True)
    clone_to(ME, "victor", P / "victor")
    commit(P / "victor", "v.txt", "v\n", "local")
    s["victor_online"] = bare_sha(gh, ME, "victor")
    # whiskey: online repo created empty, cloned, committed, never pushed (upstream shows [gone])
    gh.add(ME, "whiskey")
    clone_to(ME, "whiskey", P / "whiskey")
    s["whiskey"] = commit(P / "whiskey", "w.txt", "w\n", "first, never pushed")
    # xray: ssh-style origin, repo transferred -> origin rewritten in the same ssh style
    seed(gh, ME, "xray", work)
    run_git(None, "clone", "-q", f"git@github.com:{ME}/xray.git", str(P / "xray"))
    gh.move(ME, "xray", ORG, "xray")
    # yankee: a branch someone pushed and later deleted online; P52 + NAS only hold it as origin/tmp
    wy = seed(gh, ME, "yankee", work)
    clone_to(ME, "yankee", P / "yankee")
    clone_to(ME, "yankee", N / "yankee")
    run_git(wy, "switch", "-q", "-c", "tmp")
    s["yankee_tmp"] = commit(wy, "tmp.txt", "tmp\n", "tmp work")
    run_git(wy, "push", "-q", "origin", "tmp")
    run_git(P / "yankee", "fetch", "-q", "origin")
    run_git(N / "yankee", "fetch", "-q", "origin")
    run_git(wy, "push", "-q", "origin", "--delete", "tmp")
    # zed: someone else's repo on another git host -> pull only + mirrored
    (w.other_root / "team").mkdir(parents=True)
    run_git(None, "init", "--bare", "-q", "-b", "main", str(w.other_root / "team" / "zed.git"))
    wz = work / "zed-up"
    run_git(None, "clone", "-q", "https://git.example.org/team/zed.git", str(wz))
    commit(wz, "z.txt", "z\n", "z1")
    run_git(wz, "push", "-q", "origin", "HEAD:refs/heads/main")
    run_git(None, "clone", "-q", "https://git.example.org/team/zed.git", str(P / "zed"))
    s["zed"] = commit(wz, "z2.txt", "z2\n", "z2")
    run_git(wz, "push", "-q", "origin", "HEAD:refs/heads/main")
    # detachy: P52 on a detached HEAD, main behind -> main moved, HEAD left alone; NAS detached -> reset
    wd = seed(gh, ME, "detachy", work)
    clone_to(ME, "detachy", P / "detachy")
    clone_to(ME, "detachy", N / "detachy")
    run_git(P / "detachy", "checkout", "-q", "--detach", "HEAD")
    run_git(N / "detachy", "checkout", "-q", "--detach", "HEAD")
    s["detachy_head"] = sha(P / "detachy")
    s["detachy"] = push_online(wd, "d.txt", "d\n", "online")
    # busybee: P52 stuck mid-merge -> left alone, reported
    wb = seed(gh, ME, "busybee", work, files={"f.txt": "base\n"})
    clone_to(ME, "busybee", P / "busybee")
    commit(P / "busybee", "f.txt", "local\n", "local")
    push_online(wb, "f.txt", "online\n", "online")
    run_git(P / "busybee", "pull", "-q", "--no-rebase", "origin", "main", check=False)
    s["busybee_merging"] = (P / "busybee" / ".git" / "MERGE_HEAD").exists()
    # nasonly: a never-online repo that only exists on the NAS -> published from there, cloned to P52
    no = N / "nasonly"
    run_git(None, "init", "-q", "-b", "main", str(no))
    s["nasonly"] = commit(no, "n.txt", "n\n", "only on the nas")
    # oldname: renamed online (same account) -> origin follows, folder keeps its name
    seed(gh, ME, "oldname", work)
    clone_to(ME, "oldname", P / "oldname")
    clone_to(ME, "oldname", N / "oldname")
    gh.move(ME, "oldname", ME, "newname")
    # mangled: the old script rewrote origin to a kebab-case name that never existed online
    seed(gh, ME, "Cool_Project", work)
    clone_to(ME, "Cool_Project", P / "cool-project")
    run_git(P / "cool-project", "remote", "set-url", "origin", f"https://github.com/{ME}/cool-project.git")
    # samehist: origin says whisprer/samehist (404), but the same history lives at orgA/samehist
    seed(gh, ORG, "samehist", work)
    clone_to(ORG, "samehist", P / "samehist")
    run_git(P / "samehist", "remote", "set-url", "origin", f"https://github.com/{ME}/samehist.git")
    return s


def review_regressions(tmp: Path) -> None:
    """Holes an independent review found in the destructive paths - each one reproduced,
    fixed, and pinned here so it can't come back."""
    print("\n== safety regressions")
    try:
        w = World(tmp)
        ENV["REPO_SYNC_WIP_MAX_FILE_MB"] = "0.001"  # tiny cap: proves NAS/delete backups are never capped
        gh, P, N, work = w.gh, w.p52, w.nas, w.work
        # NAS: commit made on a detached HEAD
        seed(gh, ME, "adet", work)
        clone_to(ME, "adet", P / "adet")
        clone_to(ME, "adet", N / "adet")
        run_git(N / "adet", "checkout", "-q", "--detach")
        c_adet = commit(N / "adet", "nas-only.txt", "made on the NAS\n", "detached work")
        # NAS: two stash entries
        seed(gh, ME, "bstash", work)
        clone_to(ME, "bstash", P / "bstash")
        clone_to(ME, "bstash", N / "bstash")
        (N / "bstash" / "README.md").write_text("older stash\n", encoding="utf-8")
        run_git(N / "bstash", "stash", "-q")
        (N / "bstash" / "README.md").write_text("newer stash\n", encoding="utf-8")
        run_git(N / "bstash", "stash", "-q")
        stashes_before = run_git(N / "bstash", "reflog", "show", "--format=%H", "refs/stash").stdout.split()
        # NAS: big untracked file + a small edit
        seed(gh, ME, "cbig", work)
        clone_to(ME, "cbig", P / "cbig")
        clone_to(ME, "cbig", N / "cbig")
        (N / "cbig" / "big.bin").write_bytes(b"\x01" * 50000)
        (N / "cbig" / "notes.txt").write_text("small NAS edit\n", encoding="utf-8")
        # P52: branch deleted online whose old tracking ref is AHEAD of the local branch
        wd = seed(gh, ME, "dgone", work)
        clone_to(ME, "dgone", P / "dgone")
        d = P / "dgone"
        run_git(d, "switch", "-q", "-c", "feat")
        run_git(d, "push", "-q", "-u", "origin", "feat")
        run_git(d, "switch", "-q", "main")
        run_git(wd, "fetch", "-q", "origin")
        run_git(wd, "switch", "-q", "-c", "feat", "origin/feat")
        c_dgone = commit(wd, "b.txt", "pushed from the other laptop\n", "feat work elsewhere")
        run_git(wd, "push", "-q", "origin", "feat")
        run_git(d, "fetch", "-q", "origin")            # autofetch: origin/feat moves, feat doesn't
        run_git(wd, "push", "-q", "origin", "--delete", "feat")
        # repo deleted online; only the NAS copy still has origin/exp
        wf = seed(gh, ME, "fgone", work)
        run_git(wf, "switch", "-q", "-c", "exp")
        c_fgone = commit(wf, "e.txt", "experiment\n", "exp")
        run_git(wf, "push", "-q", "origin", "exp")
        clone_to(ME, "fgone", P / "fgone")
        clone_to(ME, "fgone", N / "fgone")
        run_git(P / "fgone", "branch", "-q", "-dr", "origin/exp")
        gh.delete(ME, "fgone")
        # repo deleted online; uncommitted edit to a tracked file under a folder called 'target'
        seed(gh, ME, "gcache", work, files={"README.md": "# g\n", "src/target/Main.java": "class Main {}\n"})
        clone_to(ME, "gcache", P / "gcache")
        (P / "gcache" / "src/target/Main.java").write_text("class Main { /* a day of work */ }\n", encoding="utf-8")
        gh.delete(ME, "gcache")
        # P52: ignored .env that GitHub starts tracking
        wh = seed(gh, ME, "hignore", work, files={"README.md": "# h\n", ".gitignore": ".env\n"})
        clone_to(ME, "hignore", P / "hignore")
        (P / "hignore" / ".env").write_text("SECRET=my-real-key\n", encoding="utf-8")
        (wh / ".env").write_text("SECRET=changeme\n", encoding="utf-8")
        run_git(wh, "add", "-f", ".env")
        run_git(wh, "commit", "-q", "-m", "env template")
        run_git(wh, "push", "-q", "origin", "HEAD:refs/heads/main")
        # P52: a LICENSE.md you wrote yourself (not the template) next to a stamped README
        seed(gh, ME, "ilicense", work, files={"README.md": "# ilicense\n\nbody\n"})
        clone_to(ME, "ilicense", P / "ilicense")
        stamp_readme(P / "ilicense", "ilicense", w.docs)
        (P / "ilicense" / "LICENSE.md").write_text("My own licence terms.\n", encoding="utf-8")
        before_ilicense = bare_sha(gh, ME, "ilicense")
        # P52: a big uncommitted file -> the (capped) WIP snapshot isn't redone every run
        seed(gh, ME, "jwip", work)
        clone_to(ME, "jwip", P / "jwip")
        (P / "jwip" / "huge.dat").write_bytes(b"\x02" * 50000)

        out, plan, rep = w.run(expect_rc=(0, 1))
        scratch = tmp / "x"
        scratch.mkdir()
        check(not reachable(N / "adet", c_adet) and commit_in_backups(w.bk, "adet", c_adet, scratch),
              "NAS detached-HEAD commit bundled before the reset")
        stashes_after = run_git(N / "bstash", "reflog", "show", "--format=%H", "refs/stash", check=False).stdout.split()
        check(stashes_after == stashes_before and not steps_of(rep, "bstash"), "NAS stashes left alone (not cleared)")
        cz = zips(w.bk, r"cbig__NAS")
        big_in = False
        if cz:
            with zipfile.ZipFile(cz[0]) as zf:
                big_in = "worktree/big.bin" in zf.namelist()
        check(big_in and not (N / "cbig" / "big.bin").exists(), "NAS reset backup is never size-capped")
        check(commit_in_backups(w.bk, "dgone", c_dgone, scratch) and "feat" not in
              run_git(P / "dgone", "branch", "--format=%(refname:short)").stdout.split(),
              "gone branch: commits only on its old tracking ref were bundled first")
        fz = zips(w.bk, r"fgone")
        check(not (P / "fgone").exists() and not (N / "fgone").exists() and len(fz) == 2
              and commit_in_backups(w.bk, "fgone", c_fgone, scratch),
              "deleted repo: copies that differ get their own full backups (NAS-only ref kept)")
        gz = zips(w.bk, r"gcache")
        g_ok = False
        if gz:
            with zipfile.ZipFile(gz[0]) as zf:
                if "gcache/src/target/Main.java" in zf.namelist():
                    g_ok = "day of work" in zf.read("gcache/src/target/Main.java").decode()
        check(g_ok, "full backup keeps real files inside a folder named 'target'")
        check((P / "hignore" / ".env").read_text(encoding="utf-8") == "SECRET=my-real-key\n"
              and any(i["kind"] == "failed" for i in issues_of(rep, "hignore")),
              "fast-forward refuses to overwrite your ignored .env (reported instead)")
        check(bare_sha(gh, ME, "ilicense") == before_ilicense and (P / "ilicense" / "LICENSE.md").exists(),
              "your own LICENSE.md isn't committed as an 'old stamp'")
        jz = zips(w.bk, r"jwip__P52__wip")
        j_partial = False
        if jz:
            with zipfile.ZipFile(jz[0]) as zf:
                j_partial = "worktree/huge.dat" not in zf.namelist()
        out2, plan2, rep2 = w.run(expect_rc=(0, 1))
        check(len(jz) == 1 and j_partial and len(zips(w.bk, r"jwip__P52__wip")) == 1,
              "capped WIP snapshot (big file listed, not zipped) recorded once, not redone every run")
        cz2 = zips(w.bk, r"cbig__NAS")
        check(len(cz2) == 1, "NAS extras zip not duplicated on the second run")

        print("\n== dupes regressions")
        arch = tmp / "archive"
        arch.mkdir()
        wl = seed(gh, ME, "alive", work)
        clone_to(ME, "alive", P / "alive")
        run_git(wl, "switch", "-q", "-c", "old")
        c_old = commit(wl, "u.txt", "u\n", "old work")
        run_git(wl, "push", "-q", "origin", "old")
        clone_to(ME, "alive", arch / "e-remote")            # only unique commit: origin/old
        run_git(wl, "push", "-q", "origin", "--delete", "old")
        z = arch / "e-detached"
        run_git(None, "init", "-q", "-b", "main", str(z))
        commit(z, "x.txt", "x\n", "one")
        commit(z, "d.txt", "d\n", "only commit worth keeping")
        run_git(z, "checkout", "-q", "--detach")
        run_git(z, "branch", "-q", "-D", "main")
        run_git(P / "alive", "switch", "-q", "-c", "x")
        commit(P / "alive", "x.txt", "x\n", "x work")
        run_git(P / "alive", "switch", "-q", "main")
        shutil.copytree(P / "alive", arch / "j-dangling")    # x only survives as a dangling object in live
        run_git(P / "alive", "branch", "-q", "-D", "x")
        clone_to(ME, "alive", arch / "k-contained")           # genuinely nothing new
        arch2 = tmp / "archive2"
        arch2.mkdir()
        q = arch2 / "only-copy"
        run_git(None, "init", "-q", "-b", "main", str(q))
        commit(q, "q.txt", "unique\n", "unique work")
        roots = ["--root", str(arch), "--root", str(arch2)]
        try:
            os.symlink(arch2, tmp / "archive2-link", target_is_directory=True)
            roots += ["--root", str(tmp / "archive2-link")]
        except OSError:
            pass
        cmd = [sys.executable, str(SCRIPT), "dupes", "--p52-base", str(P), "--nas-base", str(N)] + roots + ["--apply", "--yes"]
        pr = subprocess.run(cmd, env=ENV, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8")
        check((arch / "e-remote").exists() and not reachable(P / "alive", c_old), "dupes keeps a copy whose only unique commit is a remote-tracking ref")
        check((arch / "e-detached").exists(), "dupes keeps a copy with commits only on a detached HEAD")
        check((arch / "j-dangling").exists(), "dupes: a dangling object in the live repo doesn't count as 'contained'")
        check(q.exists(), "dupes: the same folder reached two ways is never 'identical to itself'")
        check(not (arch / "k-contained").exists(), "dupes still removes a copy that truly holds nothing new")
        if pr.returncode not in (0, 1):
            print(pr.stdout)
    finally:
        ENV.pop("REPO_SYNC_WIP_MAX_FILE_MB", None)


def review2_regressions(tmp: Path) -> None:
    """Second review: classification + Windows + idempotence holes, pinned."""
    print("\n== classification regressions")
    w = World(tmp)
    gh, P, N, work = w.gh, w.p52, w.nas, w.work
    # a dry run (or a cancelled YES) must not turn a never-created repo into "deleted on GitHub"
    np_ = P / "newproj"
    run_git(None, "init", "-q", "-b", "main", str(np_))
    commit(np_, "n.txt", "n\n", "first")
    run_git(np_, "remote", "add", "origin", f"https://github.com/{ME}/newproj.git")
    shutil.copytree(np_, N / "newproj")
    # two different scp-style origins with absolute paths must stay two different repos
    for folder, base, path in (("xproj", P, "/volume1/git/x.git"), ("yproj", N, "/volume1/git/y.git")):
        run_git(None, "init", "-q", "-b", "main", str(base / folder))
        commit(base / folder, "f.txt", folder + "\n", folder)
        run_git(base / folder, "remote", "add", "origin", f"admin@nas.invalid:{path}")
    # an SSH alias from ~/.ssh/config ("gh" -> github.com) is still your GitHub repo
    ssh_cfg = tmp / "ssh_config"
    ssh_cfg.write_text("Host gh\n  HostName github.com\n  User git\n", encoding="utf-8")
    ENV["REPO_SYNC_SSH_CONFIG"] = str(ssh_cfg)
    gc = Path(ENV["GIT_CONFIG_GLOBAL"])
    gc.write_text(gc.read_text(encoding="utf-8") +
                  f"[url \"{w.gh_root.as_uri()}/\"]\n\tinsteadOf = gh:\n", encoding="utf-8")
    seed(gh, ME, "alias2", work)
    run_git(None, "clone", "-q", f"gh:{ME}/alias2.git", str(P / "alias2"))
    alias_local = commit(P / "alias2", "l.txt", "l\n", "local work")
    # an old name reused online for a NEW repo with unrelated history
    old = P / "reused"
    run_git(None, "init", "-q", "-b", "main", str(old))
    commit(old, "old.txt", "old project\n", "old project")
    run_git(old, "remote", "add", "origin", f"https://github.com/{ME}/reused.git")
    run_git(old, "update-ref", "refs/remotes/origin/main", "HEAD")
    run_git(old, "branch", "-q", "--set-upstream-to=origin/main", "main")
    run_git(old, "branch", "-q", "side")
    run_git(old, "update-ref", "refs/remotes/origin/side", "HEAD")
    run_git(old, "branch", "-q", "--set-upstream-to=origin/side", "side")
    old_head = sha(old)
    seed(gh, ME, "reused", work, files={"new.txt": "brand new repo\n"})
    # deleted online: an EMPTY git-init folder of the same name on the NAS isn't swept up with it
    seed(gh, ME, "gone2", work)
    clone_to(ME, "gone2", P / "gone2")
    run_git(None, "init", "-q", "-b", "main", str(N / "gone2"))
    (N / "gone2" / "notes.txt").write_text("unrelated notes, never committed\n", encoding="utf-8")
    gh.delete(ME, "gone2")
    # a brand-new online repo whose name is taken by a plain folder on the P52 only
    seed(gh, ME, "samename", work)
    (P / "samename").mkdir()
    (P / "samename" / "readme.txt").write_text("not a repo\n", encoding="utf-8")

    out, plan, rep = w.run(dry=True)
    out, plan, rep = w.run(expect_rc=(0, 1))
    check(f"{ME}/newproj" in gh.repos and np_.is_dir() and (N / "newproj").is_dir(),
          "after a dry run, a never-created repo is still published (not treated as deleted)")
    xs = [r for r in plan["results"] if "xproj" in r["display"] or "yproj" in r["display"]
          or any("xproj" in st["detail"] or "yproj" in st["detail"] for st in r["steps"])]
    check(not any(st["kind"] == "rename-folder" for r in xs for st in r["steps"])
          and (P / "xproj").is_dir() and (N / "yproj").is_dir(),
          "unrelated absolute-path scp origins stay separate (no bogus rename)")
    check(bare_sha(gh, ME, "alias2") == alias_local and not (P / f"alias2--{ME}").exists(),
          "SSH alias origin recognised as your repo: pushed, no second clone")
    check(sha(old) == old_head and run_git(old, "rev-parse", "-q", "--verify", "refs/heads/side", check=False).stdout.strip()
          and any(i["kind"] == "unrelated" for i in issues_of(rep, "reused")),
          "origin reused by an unrelated repo: nothing deleted or pushed, reported")
    check((N / "gone2" / "notes.txt").exists() and not (P / "gone2").exists(),
          "deleted repo's P52 copy removed, unrelated empty NAS folder of the same name kept")
    check((P / f"samename--{ME}").is_dir() and (N / f"samename--{ME}").is_dir() and not (N / "samename").exists(),
          "new clones get the same folder name on the P52 and the NAS")
    out, plan, rep = w.run(expect_rc=(0, 1))
    check(not any(st["kind"] == "rename-folder" for r in plan["results"] for st in r["steps"]),
          "no rename churn on the next run")

    print("\n== lock")
    lock = w.state / "repo-sync.lock"
    lock.write_text("12345 someone-else\n", encoding="utf-8")
    cmd = [sys.executable, str(SCRIPT), "sync", "--p52-base", str(P), "--nas-base", str(N), "--backup-root",
           str(w.bk), "--state-dir", str(w.state), "--dry-run"]
    pr = subprocess.run(cmd, env=ENV, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8")
    check(pr.returncode != 0 and "Another repo_sync run is active" in pr.stdout and lock.exists(),
          "a live run's lock is respected (and not removed)")
    old_t = __import__("time").time() - 3600
    os.utime(lock, (old_t, old_t))
    pr = subprocess.run(cmd, env=ENV, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8")
    check(pr.returncode == 0 and not lock.exists(), "a dead run's stale lock is taken over and cleaned up")
    ENV.pop("REPO_SYNC_SSH_CONFIG", None)


def main() -> int:
    unit_checks()
    tmp = Path(tempfile.mkdtemp(prefix="repo-sync-test-"))
    try:
        w = World(tmp)
        gh, P, N = w.gh, w.p52, w.nas
        s = build_world(w)

        print("\n== run 1: dry run changes nothing")
        before = {p.name: sha(p) for p in P.iterdir() if (p / ".git").is_dir() and run_git(p, "rev-parse", "-q", "--verify", "HEAD", check=False).returncode == 0}
        out, plan, _ = w.run(dry=True)
        after = {p.name: sha(p) for p in P.iterdir() if (p / ".git").is_dir() and run_git(p, "rev-parse", "-q", "--verify", "HEAD", check=False).returncode == 0}
        check(before == after, "dry run left every P52 HEAD alone")
        check(not (P / "bravo").exists() and not w.bk.exists(), "dry run cloned nothing, wrote no backups")
        kinds = {st["kind"] for r in plan["results"] for st in r["steps"]}
        check({"clone", "ff", "push", "retarget", "create-online", "delete-repo", "nas-reset", "commit-stamps"} <= kinds,
              "plan contains every kind of change")

        print("\n== run 2: apply")
        out, plan, rep = w.run()
        failed = [f"{r['display']}: {st['detail']} -> {st['error']}" for r in rep["results"] for st in r["steps"]
                  if st["status"] not in ("done",)]
        check(not failed, "every applied step succeeded" + (f" (not: {failed})" if failed else ""))
        check((P / "bravo").is_dir() and (N / "bravo").is_dir(), "bravo cloned to P52 and NAS")
        check(sha(P / "charlie") == s["charlie"] and sha(N / "charlie") == s["charlie"], "charlie fast-forwarded on both")
        check(bare_sha(gh, ME, "delta") == s["delta"], "delta: P52 commit pushed to GitHub")
        check(sha(N / "delta") == s["delta"], "delta: NAS pulled the pushed commit in the same run")
        check(sha(P / "echo") == s["echo_local"] and bare_sha(gh, ME, "echo") == s["echo_online"],
              "echo: diverged, nothing forced either side")
        check(any(i["kind"] == "diverged" for i in issues_of(rep, "whisprer/echo")), "echo: reported as diverged")
        check(sha(P / "foxtrot") == s["foxtrot"] and (P / "foxtrot" / "scratch.txt").exists(),
              "foxtrot: fast-forwarded around the uncommitted file, file kept")
        check(len(zips(w.bk, r"whisprer__foxtrot__P52__wip")) == 1, "foxtrot: WIP backup made")
        check(sha(P / "golf") == s["golf_head"] and "my local edit" in (P / "golf" / "shared.txt").read_text(),
              "golf: blocked fast-forward left branch and edit alone")
        check(any(i["kind"] == "ff-blocked" for i in issues_of(rep, "whisprer/golf")), "golf: reported ff-blocked")
        check(origin(P / "hotel").endswith(f"{ORG}/hotel.git") and origin(N / "hotel").endswith(f"{ORG}/hotel.git"),
              "hotel: moved repo's origin fixed on both")
        check(not (P / "india").exists() and not (N / "india").exists(), "india: deleted online -> removed locally")
        iz = zips(w.bk, r"india")
        check(len(iz) == 1, f"india: exactly one full backup for two identical copies (got {len(iz)})")
        if iz:
            with zipfile.ZipFile(iz[0]) as zf:
                names = zf.namelist()
            check(any(n.startswith("india/.git/") for n in names) and "india/README.md" in names,
                  "india: backup holds the whole repo incl. .git")
        check(f"{ME}/juliet".lower() in gh.repos and gh.repos[f"{ME}/juliet"]["private"], "juliet: private repo created")
        check(bare_sha(gh, ME, "juliet") == s["juliet"] and bare_sha(gh, ME, "juliet", "refs/heads/side"),
              "juliet: all branches pushed")
        check(sha(N / "juliet") == s["juliet"] and is_clean(N / "juliet"), "juliet: NAS copy now matches GitHub")
        check(origin(P / "kilo").endswith(f"{ME}/kilo.git"), "kilo: no-origin folder matched by history, origin set")
        check(sha(P / "lima") == s["lima"] and (N / "lima").is_dir(), "lima: third-party pulled + mirrored to NAS")
        check(sha(P / "mike-copy") == s["mike_copy"] and any(i["kind"] == "duplicate" for i in issues_of(rep, "whisprer/mike")),
              "mike: duplicate folder untouched and reported")
        check(origin(P / "november").endswith(f"{ME}/november.git")
              and origin(P / f"november--{ORG}").endswith(f"{ORG}/november.git")
              and (N / f"november--{ORG}").is_dir(), "november: same-name repos get distinct folders")
        br = run_git(P / "oscar", "branch", "--format=%(refname:short)").stdout.split()
        check("feat" not in br and "feat2" not in br and "main" in br, "oscar: branches deleted online removed")
        oz = zips(w.bk, r"oscar__P52__branch-feat2")
        check(len(oz) == 1, "oscar: feat2's unique commit backed up as a bundle first")
        if oz:
            with zipfile.ZipFile(oz[0]) as zf:
                check("extras.bundle" in zf.namelist(), "oscar: backup contains the bundle")
        check(bare_sha(gh, ME, "papa", "refs/heads/newbranch") == s["papa"], "papa: new local branch published")
        qlog = run_git(gh.bare(ME, "quebec"), "log", "-1", "--format=%s", "main").stdout.strip()
        check(qlog.startswith("docs: add standard README header"), "quebec: stamp commit pushed")
        check(is_clean(P / "quebec") and is_clean(N / "quebec") and sha(N / "quebec") == bare_sha(gh, ME, "quebec"),
              "quebec: P52 + NAS clean and level with GitHub")
        check(not zips(w.bk, r"quebec__NAS"), "quebec: NAS stamps discarded without a pointless backup")
        rb = run_git(N / "romeo", "branch", "--format=%(refname:short)").stdout.split()
        check("nas-junk" not in rb and is_clean(N / "romeo"), "romeo: NAS extras removed")
        rz = zips(w.bk, r"romeo__NAS__nas-extras")
        check(len(rz) == 1, "romeo: NAS extras backed up first")
        if rz:
            with zipfile.ZipFile(rz[0]) as zf:
                nm = zf.namelist()
            check("extras.bundle" in nm and "worktree/README.md" in nm, "romeo: backup has bundle + edited file")
        check((N / "sierra").is_dir() and not (N / "Sierra-Old").exists(), "sierra: NAS folder renamed to match P52")
        check(sha(N / "tango") == s["tango"], "tango: NAS copy with other owner synced"
              + (" (dubious-ownership path exercised)" if s["tango_chowned"] else " (ownership test skipped)"))
        check(f"{ME}/uniform" in gh.repos and bare_sha(gh, ME, "uniform"), "uniform: never-created origin -> created + pushed")
        check(bare_sha(gh, ME, "victor") == s["victor_online"]
              and any(i["kind"] == "archived" for i in issues_of(rep, "whisprer/victor")),
              "victor: archived repo not pushed, reported")
        check(bare_sha(gh, ME, "whiskey") == s["whiskey"], "whiskey: never-pushed branch of an empty clone published")
        check(sha(N / "whiskey") == s["whiskey"], "whiskey: NAS mirror has it")
        check(origin(P / "xray") == f"git@github.com:{ORG}/xray.git", "xray: ssh origin retargeted, ssh style kept")
        yref = run_git(P / "yankee", "rev-parse", "-q", "--verify", "refs/remotes/origin/tmp", check=False).stdout.strip()
        nref = run_git(N / "yankee", "rev-parse", "-q", "--verify", "refs/remotes/origin/tmp", check=False).stdout.strip()
        check(not yref and not nref, "yankee: branch deleted online forgotten on P52 + NAS")
        yz = zips(w.bk, r"yankee__(P52|NAS)__deleted-online")
        check(len(yz) == 1, f"yankee: its commit backed up once (P52 + NAS copies are identical -> one zip, got {len(yz)})")
        if yz:
            with zipfile.ZipFile(yz[0]) as zf:
                check("extras.bundle" in zf.namelist(), "yankee: the backup is a git bundle")
        check(sha(P / "zed") == s["zed"] and (N / "zed").is_dir(), "zed: other-host repo pulled + mirrored")
        check(sha(P / "detachy") == s["detachy_head"] and sha(P / "detachy", "refs/heads/main") == s["detachy"],
              "detachy: P52 main fast-forwarded, detached HEAD left alone")
        check(run_git(N / "detachy", "symbolic-ref", "-q", "HEAD", check=False).stdout.strip() == "refs/heads/main"
              and sha(N / "detachy") == s["detachy"], "detachy: NAS detached HEAD reset to main")
        check(s["busybee_merging"] and (P / "busybee" / ".git" / "MERGE_HEAD").exists()
              and any(i["kind"] == "busy" for i in issues_of(rep, "busybee")), "busybee: mid-merge repo left alone, reported")
        check(bare_sha(gh, ME, "nasonly") == s["nasonly"] and sha(P / "nasonly") == s["nasonly"],
              "nasonly: NAS-only repo published, then cloned to P52")
        check(origin(P / "oldname").endswith(f"{ME}/newname.git") and origin(N / "oldname").endswith(f"{ME}/newname.git"),
              "oldname: renamed online -> origin follows, folder name kept")
        check((P / "cool-project").is_dir() and origin(P / "cool-project").endswith(f"{ME}/Cool_Project.git")
              and (N / "cool-project").is_dir(), "mangled origin name: NOT treated as deleted, origin repaired")
        check((P / "samehist").is_dir() and origin(P / "samehist").endswith(f"{ORG}/samehist.git"),
              "same history in another account: bound there, not deleted")
        check((P / "alpha").is_dir() and not steps_of(rep, "whisprer/alpha"), "alpha: already agreed, untouched")
        mf = w.bk / "manifest.jsonl"
        check(mf.exists() and len(mf.read_text().splitlines()) == len(list(w.bk.rglob("*.zip"))),
              "manifest lists every backup zip")

        print("\n== run 3: second run changes nothing (idempotent)")
        nzip = len(list(w.bk.rglob("*.zip")))
        out, plan, rep = w.run()
        leftover = [f"{r['display']}: {st['detail']}" for r in plan["results"] for st in r["steps"]]
        check(not leftover, "no steps planned on the second run" + (f" (got: {leftover[:6]})" if leftover else ""))
        check(len(list(w.bk.rglob("*.zip"))) == nzip, "no duplicate backups on the second run")

        print("\n== run 4: brakes")
        gh.delete(ME, "alpha")
        out, plan, rep = w.run("--max-deletes", "0")
        check((P / "alpha").is_dir() and (N / "alpha").is_dir(), "brakes: nothing deleted over the limit")
        check(any(i["kind"] == "brakes" for i in issues_of(plan, "alpha")), "brakes: reported")
        out, plan, rep = w.run()
        check(not (P / "alpha").exists() and zips(w.bk, r"alpha"), "within the limit: backed up then deleted")

        print("\n== run 4b: policies (--local-only keep/archive, --stamps discard)")
        k = P / "keeper"
        run_git(None, "init", "-q", "-b", "main", str(k))
        commit(k, "k.txt", "k\n", "k")
        out, plan, rep = w.run("--local-only", "keep", "--only", "keeper")
        check(k.is_dir() and f"{ME}/keeper" not in gh.repos and not all_steps(plan), "--local-only keep: left alone")
        out, plan, rep = w.run("--local-only", "archive", "--only", "keeper")
        check(not k.exists() and zips(w.bk, r"keeper__P52__never-online"), "--local-only archive: backed up + removed")
        stamp_readme(P / "charlie", "charlie", w.docs)
        out, plan, rep = w.run("--stamps", "discard", "--only", "charlie")
        check(is_clean(P / "charlie") and zips(w.bk, r"charlie__P52__stamps"), "--stamps discard: stamps backed up + removed")

        print("\n== run 4c: something changes between the plan and YES")
        commit(P / "papa", "p2.txt", "p2\n", "planned push")

        def sneak_papa() -> None:
            run_git(P / "papa", "switch", "-q", "-c", "sneaky")        # appears after the plan was shown
            commit(P / "papa", "s.txt", "s\n", "not in the plan")
            run_git(P / "papa", "switch", "-q", "main")
        g = w.run_interrupted(sneak_papa, "--only", "papa")
        check(bare_sha(gh, ME, "papa") == sha(P / "papa", "refs/heads/main"), "gate: the planned push happened")
        check(not bare_sha(gh, ME, "papa", "refs/heads/sneaky")
              and any(i["kind"] == "changed" for i in issues_of(g, "papa")),
              "gate: a push that wasn't in the plan was NOT done, and was reported")
        planned_tip = commit(P / "papa", "p3.txt", "p3\n", "planned")
        g = w.run_interrupted(lambda: commit(P / "papa", "p4.txt", "p4\n", "made while you read the plan"),
                              "--only", "papa")
        check(bare_sha(gh, ME, "papa") != planned_tip and bare_sha(gh, ME, "papa") != sha(P / "papa")
              and any(i["kind"] == "changed" for i in issues_of(g, "papa")),
              "gate: a commit made while the plan was on screen stops that push (nothing unseen goes out)")
        n0 = w.gh.next_id
        lo = P / "latecomer"
        run_git(None, "init", "-q", "-b", "main", str(lo))
        commit(lo, "a.txt", "a\n", "first")
        g = w.run_interrupted(lambda: commit(lo, "b.txt", "b\n", "while you read"), "--only", "latecomer")
        check(f"{ME}/latecomer" not in gh.repos and w.gh.next_id == n0
              and any(i["kind"] == "changed" for i in issues_of(g, "latecomer")),
              "gate: never-online repo that changed after the plan -> nothing created, no empty repo left")

        print("\n== run 5: dupes cleanup")
        arch = tmp / "old-archive"
        arch.mkdir()
        clone_to(ME, "bravo", arch / "20250101__bravo__merged-source-repo")           # contained in live
        clone_to(ME, "charlie", arch / "copy-with-work")
        commit(arch / "copy-with-work", "u.txt", "unique\n", "unique")                     # has unique commit
        z = arch / "zulu-1"
        run_git(None, "init", "-q", "-b", "main", str(z))
        commit(z, "z.txt", "z\n", "z")
        shutil.copytree(z, arch / "zulu-2")                                                # identical pair
        cmd = [sys.executable, str(SCRIPT), "dupes", "--p52-base", str(P), "--nas-base", str(N), "--root", str(arch)]
        p = subprocess.run(cmd, env=ENV, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8")
        check(p.returncode == 0 and "HOLD NOTHING NEW - safe to delete (2" in p.stdout, "dupes: finds 2 deletable copies")
        p = subprocess.run(cmd + ["--apply", "--yes"], env=ENV, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           text=True, encoding="utf-8")
        check(not (arch / "20250101__bravo__merged-source-repo").exists(), "dupes: contained copy deleted")
        check((arch / "copy-with-work").exists(), "dupes: copy with unique work kept")
        check((arch / "zulu-1").exists() != (arch / "zulu-2").exists(), "dupes: one of two identical copies kept")

        print("\n== run 6: safety stops")
        empty_nas = tmp / "emptynas"
        empty_nas.mkdir()
        cmd = [sys.executable, str(SCRIPT), "sync", "--p52-base", str(P), "--nas-base", str(empty_nas),
               "--backup-root", str(w.bk), "--state-dir", str(w.state), "--dry-run"]
        p = subprocess.run(cmd, env=ENV, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8")
        check(p.returncode != 0 and "No repos found" in p.stdout, "empty NAS folder stops the run")
        cmd[cmd.index(str(empty_nas))] = str(tmp / "missing")
        p = subprocess.run(cmd, env=ENV, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8")
        check(p.returncode != 0 and "not found" in p.stdout, "missing NAS folder stops the run")
        review_regressions(tmp / "review")
        review2_regressions(tmp / "review2")
    finally:
        if os.environ.get("KEEP_TEST_DIR"):
            print(f"\n(kept {tmp})")
        else:
            force_rmtree(tmp)
    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    for f in FAILED:
        print("  FAILED:", f)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
