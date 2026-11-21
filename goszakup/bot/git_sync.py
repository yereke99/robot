"""Update a launch checkout and publish report-only commits to origin/main."""

from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path
from tempfile import TemporaryDirectory


class GitSyncError(RuntimeError):
    """An error safe to display without logging credentials from Git output."""

    def __init__(self, operation: str, *, remote_advanced: bool = False):
        super().__init__(operation)
        self.remote_advanced = remote_advanced


REPORT_ROOT = "reports/windows"
REPORT_NAME = re.compile(r"(?:bid|run)_[0-9]{8}T[0-9]{6}Z_[0-9a-f]{12}\.txt")
_REMOTE_MAIN = "refs/remotes/origin/main"


def _windows_git() -> str:
    """Find Git for Windows even when the launcher's PATH is stale."""
    import winreg

    for directory in os.get_exec_path():
        candidate = Path(directory.strip('"')) / "git.exe"
        if candidate.is_absolute() and candidate.is_file():
            return str(candidate)

    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            with winreg.OpenKey(hive, r"SOFTWARE\GitForWindows") as key:
                directory, _ = winreg.QueryValueEx(key, "InstallPath")
        except OSError:
            continue
        candidate = Path(directory) / "cmd" / "git.exe"
        if candidate.is_absolute() and candidate.is_file():
            return str(candidate)

    for variable, subdirectory in (("ProgramW6432", "Git"), ("ProgramFiles", "Git"),
                                   ("ProgramFiles(x86)", "Git"), ("LOCALAPPDATA", "Programs/Git")):
        if directory := os.environ.get(variable):
            candidate = Path(directory) / subdirectory / "cmd" / "git.exe"
            if candidate.is_absolute() and candidate.is_file():
                return str(candidate)
    raise GitSyncError("git_unavailable")


def _git(
    repo: Path, *args: str, timeout: float = 5.0,
    data: bytes | None = None, index: Path | None = None,
) -> str:
    if timeout <= 0:
        raise GitSyncError(f"{args[0]}_timeout")
    env = os.environ.copy()
    for name in ("GIT_INDEX_FILE", "GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_NAMESPACE"):
        env.pop(name, None)
    env.update(GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="0", LC_ALL="C")
    if index is not None:
        env.update(
            GIT_INDEX_FILE=str(index),
            GIT_AUTHOR_NAME="Atagos Reports",
            GIT_AUTHOR_EMAIL="atagos-reports@users.noreply.github.com",
            GIT_COMMITTER_NAME="Atagos Reports",
            GIT_COMMITTER_EMAIL="atagos-reports@users.noreply.github.com",
        )
    executable = _windows_git() if os.name == "nt" else "git"
    try:
        result = subprocess.run(
            [executable, "-c", "credential.interactive=false", *args],
            cwd=repo, env=env, input=data, capture_output=True,
            timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired:
        raise GitSyncError(f"{args[0]}_timeout") from None
    except OSError:
        raise GitSyncError("git_unavailable") from None
    if result.returncode:
        output = result.stdout + result.stderr
        retry = args[0] == "push" and any(
            reason in output for reason in (b"non-fast-forward", b"fetch first", b"failed to update ref")
        )
        raise GitSyncError(f"{args[0]}_failed", remote_advanced=retry)
    return result.stdout.decode("utf-8", errors="replace").strip()


def update_main(repo: Path) -> str:
    """Start only from current remote main, preserving local edits on failure."""
    if _git(repo, "branch", "--show-current") != "main":
        raise GitSyncError("checkout_main_required")
    if _git(repo, "status", "--porcelain", "--untracked-files=no"):
        raise GitSyncError("checkout_has_tracked_changes")
    _git(repo, "pull", "--ff-only", "--no-rebase", "origin", "main", timeout=45)
    revision = _git(repo, "rev-parse", "HEAD")
    if revision != _git(repo, "rev-parse", "FETCH_HEAD"):
        raise GitSyncError("checkout_differs_from_origin_main")
    return revision


def publish_reports(repo: Path, reports: dict[str, bytes]) -> None:
    """Commit generated reports onto remote main without staging the checkout."""
    if not all(REPORT_NAME.fullmatch(filename) for filename in reports):
        raise ValueError("invalid report filename")
    if not reports:
        return
    deadline = time.monotonic() + 25.0
    with TemporaryDirectory(prefix="atagos-git-") as temporary:
        index = Path(temporary) / "report-index"

        def git(*args: str, data: bytes | None = None) -> str:
            return _git(repo, *args, data=data, index=index, timeout=deadline - time.monotonic())

        entries = []
        for name, payload in reports.items():
            blob = git("hash-object", "-w", "--stdin", data=payload)
            entries.append(f"100644 {blob}\t{REPORT_ROOT}/{name}\n")
        index_entries = "".join(entries).encode("ascii")
        for attempt in range(3):
            # Keep FETCH_HEAD available for the launcher. Only the remote-tracking
            # ref is refreshed here, even when someone has staged local edits.
            git("fetch", "--no-tags", "--no-write-fetch-head", "origin", f"refs/heads/main:{_REMOTE_MAIN}")
            parent = git("rev-parse", _REMOTE_MAIN)
            git("read-tree", parent)
            git("update-index", "--index-info", data=index_entries)
            tree = git("write-tree")
            if tree == git("rev-parse", f"{parent}^{{tree}}"):
                return
            commit = git("commit-tree", "--no-gpg-sign", tree, "-p", parent,
                         "-m", "Publish Windows diagnostic reports")
            try:
                git("push", "--porcelain", "--no-follow-tags", "origin", f"{commit}:refs/heads/main")
                return
            except GitSyncError as exc:
                if not exc.remote_advanced or attempt == 2:
                    raise
