# repo-sync

**What it is:** one command, run on the P52, that gets GitHub, the P52 and the NAS agreeing.

```
GitHub (the truth)  <->  P52  D:\code          (where new work starts)
                    <->  NAS  Z:\GitHub-Repos  (an exact mirror of GitHub)
```

**Coming back to this cold? Do this:**

1. Open PowerShell in this folder.
2. `powershell -ExecutionPolicy Bypass -File .\repo-sync.ps1`
3. It shows a **PLAN**. Read the **NEEDS YOU** list, then type `YES`.

Plans and reports are saved in `repo-sync-logs\` next to the script. Backups go to `Z:\_repo-sync-backups\`.

---

## Files

| file | what |
|---|---|
| `repo_sync.py` | the tool. Python 3.10+, standard library only (no venv, no `requests`) |
| `repo-sync.ps1` | maps `Z:` if needed, then runs the tool |
| `test_repo_sync.py` | self-test against a fake GitHub in a temp folder. It never touches your real repos, GitHub or git config |

## First time (about 10 minutes)

1. **GitHub token.** Make a *classic* personal access token with the scopes **`repo`** and **`read:org`**. Fine-grained tokens can only see one account/org at a time, and you have six. Save it:
   ```powershell
   [Environment]::SetEnvironmentVariable('GITHUB_PAT', 'ghp_xxx', 'User')
   ```
   Then open a **new** PowerShell window. If an org uses SSO, use the token's *Configure SSO* button to authorise it for that org. (No token set? The tool falls back to `gh auth token`, then to Git Credential Manager.)
2. **NAS login in Windows Credential Manager** rather than a text file (it asks for the password):
   ```powershell
   cmdkey /add:192.168.1.50 /user:wofl /pass
   ```
   Then delete the password lines from `repo_convergence.md`.
3. **Self-test:** `py -3 test_repo_sync.py`. It should end with `... passed, 0 failed`.
4. **Look before you leap:** `.\repo-sync.ps1 -DryRun`. The full plan is in `repo-sync-logs\repo-sync-<time>-plan.md`.
5. **Real run:** `.\repo-sync.ps1`, then type `YES`.

The first real run is the big one: moved origins, new private repos, and the old stamps committed. After that, a run usually shows a handful of changes, or `Nothing to change.`

## What happens to each repo

| situation | what the tool does |
|---|---|
| on GitHub, missing on P52 and/or NAS | clones it there |
| GitHub has new commits | fast-forwards the P52 and the NAS. On the P52 it never touches uncommitted files or ignored ones like `.env`. If they're in the way, it goes on NEEDS YOU |
| P52 has commits / branches GitHub hasn't | pushes them, then the NAS pulls them in the same run |
| repo moved or renamed on GitHub (your old `whisprer/...` origins) | follows GitHub's redirect and fixes `origin`. Folder names stay as they are |
| origin names a repo that now has **unrelated history** (an old name reused for a new repo) | left alone and reported. Nothing gets pushed, reset or deleted |
| folder with no origin | matched to your online repo **by shared history** (never by name alone) and gets its origin back |
| **never been on GitHub** | creates a **PRIVATE** repo under `whisprer`, pushes every branch and tag, then mirrors it to the NAS (`--local-only push`) |
| **deleted on GitHub** | full zip backup, then removes the P52 and NAS copies. Brakes go on at more than 10 in one run |
| branch deleted on GitHub | removed locally. If it holds commits that exist nowhere else, a bundle of them is backed up first |
| branch tracks a GitHub branch that never existed (e.g. you cloned an empty repo) | pushed |
| **old README badge header + template docs** as the *only* uncommitted change | committed as `docs: add standard README header and project docs` and pushed (`--stamps commit`). A doc only counts if it's byte-identical to your template in `D:\code\1-git-docs`, so a `LICENSE.md` you wrote yourself is never auto-committed |
| NAS has anything GitHub doesn't (edits, stray branches, commits on a detached HEAD) | backs up what's unique, then resets the NAS copy to match GitHub. Stashes are left alone |
| NAS folder name differs from the P52's | renamed to match the P52 |
| someone else's repo (third-party clone) | pulled, mirrored, never pushed, never deleted |
| both sides changed (diverged) | **never auto-merged**: NEEDS YOU, with the exact commands. Your commits get backed up |
| uncommitted work on the P52 | left alone. A copy goes into the backups, only when it has changed since last time |

## Backups: once per state, never copies of copies

- Every state gets **fingerprinted** (commits + file contents). If that exact state is already backed up, nothing new is written. That's why running it twice, or the P52 and NAS holding identical copies, gives you **one** zip.
- Layout: `Z:\_repo-sync-backups\<run time>\<account>__<repo>__<P52|NAS>__<reason>__<id>.zip`, with `manifest.jsonl` at the top as the index. A run that backs up nothing creates no folder.
- The tool **never deletes backups**. Pruning them is up to you.
- Backups taken before a NAS reset or a delete are **never size-capped**. The routine work-in-progress snapshots of the P52 leave out single files over 100 MB and list them in `meta.json`. Those files are still on the P52; nothing is destroyed there.

**Restoring:**

- *Full backup* (deleted repos, reason `deleted-online` / `never-online`): unzip it. The folder inside **is** the repo, `.git` and all. Only cache folders that git itself ignores (like `node_modules` and `target`) are left out.
- *Extras backup* (`wip`, `nas-extras`, `branch-...`, `deleted-online`): `worktree\` holds the changed files exactly as they were. For commits, use `extras.bundle`:
  ```powershell
  cd D:\code\some-repo
  git fetch C:\path\to\unzipped\extras.bundle "refs/*:refs/restored/*"
  git log --oneline --all     # your commits are under refs/restored/...
  ```
  `meta.json` inside every zip says what's in it and where it came from.

## Clearing out the old copies-of-copies

```powershell
.\repo-sync.ps1 -Dupes               # list only
.\repo-sync.ps1 -Dupes -- --apply    # delete after you type YES
```

This searches the NAS share and `D:\code\.repo-convergence-archive` for repo copies. It only deletes a copy when:

- every commit it can reach (branches, tags, remote-tracking refs, detached HEAD, stashes, reflog) is on a branch or tag of a live repo, and it has no uncommitted, untracked, ignored or git-hook files that differ, or
- it's an exact duplicate of another copy (one copy is kept), or
- it's an empty repo.

Anything holding something unique is listed as kept, with the reason. The live repos in `D:\code` and `Z:\GitHub-Repos` are never touched by this. For other folders: `py -3 repo_sync.py dupes --root <folder> [--apply]`.

## Safety rails

- **Nothing changes before YES.** The plan only refreshes `origin/*` tracking refs.
- **The apply does exactly what you approved, down to the commit.** A commit you make while the plan is on screen waits for the next run. If any part of a change no longer matches the plan, none of it happens (so no half-made empty repos), and it's reported.
- **Never force-pushes, never rewrites your branches, never touches uncommitted files on the P52.**
- **Deletes only after a backup** has been written, re-opened and CRC-checked, and the folder is unchanged since. The folder is renamed first, so a folder that's open in VS Code fails safely.
- **"Deleted on GitHub" needs three things to agree:** the API says 404, git says the repo doesn't exist, and the local copy had really fetched from it before (a dry run never counts). A renamed repo, a repo moved to another of your accounts, or a login problem won't pass all three. Git logins are checked before anything starts.
- **Unrelated history is hands-off:** if a copy shares no commits with the GitHub repo its origin names, nothing in it is pushed, reset or deleted.
- **Brakes:** more than 10 repo folders to delete in one run means none get deleted until you pass `--allow-mass-delete`.
- If an account's repo list can't be read, nothing of that account's is deleted that run.
- Only one run at a time (`repo-sync-logs\repo-sync.lock`).
- The NAS needs no internet: every git command runs on the P52 and writes to the share.

## Handy flags

Pass these after `--` via the wrapper (`.\repo-sync.ps1 -- --flag`), or straight to `py -3 repo_sync.py`:

| flag | effect |
|---|---|
| `--dry-run` | plan only |
| `--yes` | no prompt (scheduled runs; the brakes still apply) |
| `--only PATTERN` | just matching repos: `whisprer/foo`, `foo`, `'whisprer/*'` (repeatable) |
| `--ignore PATTERN` | skip matching repos (or add them to `IGNORE` at the top of the script) |
| `--no-push` | never push or create anything online |
| `--no-push-new-branches` | don't publish local-only branches |
| `--local-only archive` / `keep` | never-online repos: back up + delete / leave alone and list |
| `--stamps discard` / `keep` | old stamps: back up + remove / leave alone |
| `--keep-gone` / `--keep-gone-branches` | never delete local copies of repos / branches deleted on GitHub |
| `--allow-mass-delete` | release the brakes (after reading the plan) |
| `--skip-nas` | P52 <-> GitHub only |
| `--nas-base`, `--p52-base`, `--backup-root` | different folders (defaults are at the top of `repo_sync.py`) |
| `--jobs N` | repos worked on at once (default 8) |

## When something's on NEEDS YOU

| it says | do |
|---|---|
| **diverged** | `cd <repo>; git switch <branch>; git pull --rebase` (or merge), then run again |
| **ff-blocked** (your uncommitted changes are in the way) | commit or `git stash`, then `git pull --ff-only`, then `git stash pop` |
| **duplicate** folders for one repo | keep one. `-Dupes` shows which copies are identical |
| **archived** on GitHub with unpushed commits | unarchive it on GitHub, or leave it |
| **token-blind / unlisted-owner / api** | token scope or SSO: see *First time*, step 1 |
| **name-taken** | a different folder already uses that name. Rename one |
| **busy** (mid-merge/rebase) | finish or abort it, then run again |

## What changed from `repo_convergence.py`

- The truth is **GitHub**, not the P52.
- Repos are identified by **origin + history**, not folder name. It follows GitHub's redirects, which untangles the ~230 repos you moved into orgs whose origins still said `whisprer/...`. Those were behind the old script's 247 "occupied" warnings.
- No more **file-by-file merging** of look-alike folders by timestamp (it could blend two projects together, like `solver` into `frankl`). Duplicates are reported instead.
- No more **uncommitted** README/doc stamping, the reason nothing ever matched GitHub. The existing stamps get committed once.
- It **never renames repos on GitHub**. If you want a nicer name, rename it on GitHub and the tool follows.
- **NAS copies are real clones kept in sync.** They used to be one-off folder copies that never updated.
- **Backups:** fingerprinted, deduplicated zips with a manifest. They used to be a new timestamped folder copy on every merge.
- `requests`/venv dependency dropped.

## Assumptions

- Windows 11 on the P52, Git for Windows 2.30+ on PATH, Python 3.10+ (`py -3`).
- P52 repos are the top-level folders of `D:\code`, and NAS repos the top-level folders of `Z:\GitHub-Repos` (`Z:` = `\\192.168.1.50\GitHub-Repos`). If your NAS repos actually sit at the top of `Z:\`, the tool notices, stops, and tells you to use `--nas-base Z:\`.
- Your accounts are `whisprer` plus every org you belong to (see `OWNERS` at the top of the script to pin them).
- New repos use HTTPS URLs, and Git Credential Manager does the logins. SSH origins keep their style, and aliases from `~/.ssh/config` (e.g. `gh:` → github.com) are recognised.
