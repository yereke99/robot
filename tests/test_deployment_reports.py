"""Exercise the Windows launch and report workflow without external accounts."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from goszakup.bot import git_sync, run_reports, windows_run
from goszakup.bot.config import _report_chat_id
from goszakup.bot.handlers import bid
from goszakup.bot.log_capture import RuntimeLog, public_bid_report
from goszakup.timing import BidTrace, current_trace


FILENAME = "run_20261121T100000Z_0123456789ab.txt"
PAYLOAD = b"runtime: 36 seconds\nPOST ajax_public_application 1.50s\n"
TENDER = "87654321"
APPLICATION = "98765432"
DOCUMENT = "102938475"


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.test",
         "-c", "commit.gpgSign=false", *args],
        cwd=repo, check=True, capture_output=True, text=True, timeout=10,
    )
    return result.stdout.strip()


@pytest.fixture
def git_repos(tmp_path):
    remote = tmp_path / "server.git"
    repo = tmp_path / "workstation"
    repo.mkdir()
    git(tmp_path, "init", "--bare", "-b", "main", str(remote))
    git(repo, "init", "-b", "main")
    (repo / "app.py").write_text("original code\n", encoding="utf-8")
    (repo / ".gitignore").write_text(".env\n", encoding="utf-8")
    git(repo, "add", "app.py", ".gitignore")
    git(repo, "commit", "-m", "Initial code")
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "push", "origin", "main")
    return repo, remote


def test_publish_stages_only_the_generated_report(git_repos):
    repo, remote = git_repos
    baseline = git(repo, "rev-parse", "HEAD")
    (repo / "app.py").write_text("local code changes\n", encoding="utf-8")
    (repo / ".env").write_text("TG_BOT_TOKEN=fake-local-secret\n", encoding="utf-8")
    git(repo, "add", "app.py")
    before = git(repo, "status", "--porcelain")

    git_sync.publish_reports(repo, {FILENAME: PAYLOAD})
    first = git(remote, "rev-parse", "main")
    git_sync.publish_reports(repo, {FILENAME: PAYLOAD})

    assert git(remote, "rev-parse", "main") == first
    assert git(repo, "rev-parse", "HEAD") == baseline
    assert git(repo, "status", "--porcelain") == before
    path = f"reports/windows/{FILENAME}"
    assert git(remote, "diff", "--name-only", baseline, "main") == path
    assert git(remote, "show", f"main:{path}") == PAYLOAD.decode().strip()
    assert git(remote, "show", "main:app.py") == "original code"


def test_publish_retries_on_a_simultaneous_main_update(git_repos, monkeypatch):
    repo, remote = git_repos
    other = repo.parent / "other"
    git(repo.parent, "clone", str(remote), str(other))
    (other / "app.py").write_text("new upstream code\n", encoding="utf-8")
    git(other, "commit", "-am", "A separate upstream change")
    real_git = git_sync._git
    pushes = 0

    def concurrent_git(root, *args, **kwargs):
        nonlocal pushes
        if args[0] == "push":
            pushes += 1
            if pushes == 1:
                git(other, "push", "origin", "main")
        return real_git(root, *args, **kwargs)

    monkeypatch.setattr(git_sync, "_git", concurrent_git)
    git_sync.publish_reports(repo, {FILENAME: PAYLOAD})

    assert pushes == 2
    assert git(remote, "show", "main:app.py") == "new upstream code"
    assert git(remote, "show", f"main:reports/windows/{FILENAME}") == PAYLOAD.decode().strip()


@pytest.mark.parametrize(("diagnostic", "code"), [
    (b"fatal: could not read Username: terminal prompts disabled", "push_authentication_required"),
    (b"remote: Write access to repository not granted.", "push_access_denied"),
    (b"remote: error: GH013: Repository rule violations", "push_branch_rejected"),
    (b"fatal: Could not resolve host: github.com", "push_network_failed"),
])
def test_push_errors_report_only_a_safe_cause(tmp_path, monkeypatch, diagnostic, code):
    rejected = subprocess.CompletedProcess(["git", "push"], 1, b"", diagnostic + b"\nsecret=value")
    monkeypatch.setattr(git_sync.subprocess, "run", Mock(return_value=rejected))
    monkeypatch.setattr(git_sync, "_windows_git", lambda: "git.exe")
    with pytest.raises(git_sync.GitSyncError) as exc:
        git_sync._git(tmp_path, "push", "origin", "main")
    assert str(exc.value) == code
    assert not exc.value.remote_advanced


def test_strict_update_accepts_origin_and_rejects_local_changes(git_repos):
    repo, remote = git_repos
    git_sync.publish_reports(repo, {FILENAME: PAYLOAD})
    (repo / ".env").write_text("local config\n", encoding="utf-8")
    assert git_sync.update_main(repo) == git(remote, "rev-parse", "main")
    assert (repo / ".env").read_text(encoding="utf-8") == "local config\n"
    (repo / "app.py").write_text("local code changes\n", encoding="utf-8")
    with pytest.raises(git_sync.GitSyncError, match="checkout_has_tracked_changes"):
        git_sync.update_main(repo)
    git(repo, "commit", "-am", "Local-only commit")
    local = git(repo, "rev-parse", "HEAD")
    with pytest.raises(git_sync.GitSyncError, match="checkout_differs_from_origin_main"):
        git_sync.update_main(repo)
    assert git(repo, "rev-parse", "HEAD") == local


def test_windows_launcher_requires_update_before_spawning_python(monkeypatch):
    monkeypatch.setattr(windows_run, "bot_instance", lambda _: nullcontext())
    monkeypatch.setattr(windows_run.sys, "argv", ["windows_run"])
    update = Mock(side_effect=git_sync.GitSyncError("pull_failed"))
    monkeypatch.setattr(windows_run, "update_main", update)
    prepare = Mock()
    monkeypatch.setattr(windows_run, "prepare_python", prepare)
    child = Mock()
    monkeypatch.setattr(windows_run.subprocess, "Popen", child)

    assert windows_run.main() == 1
    update.assert_called_once()
    prepare.assert_not_called()
    child.assert_not_called()


def test_prepare_python_installs_from_the_new_checkout_once(tmp_path, monkeypatch):
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("structlog>=24.1\n", encoding="utf-8")
    relative_python = "Scripts/python.exe" if os.name == "nt" else "bin/python"
    python = tmp_path / ".venv" / relative_python

    def run(command, **kwargs):
        assert kwargs["cwd"] == tmp_path
        if command[1:3] == ["-m", "venv"]:
            python.parent.mkdir(parents=True)
            python.touch()
        return subprocess.CompletedProcess(command, 0)

    launcher = Mock(side_effect=run)
    monkeypatch.setattr(windows_run.subprocess, "run", launcher)

    assert windows_run.prepare_python(tmp_path) == python
    actions = [call.args[0][1:3] for call in launcher.call_args_list]
    assert actions == [["-m", "venv"], ["-c", "import pip"], ["-m", "pip"],
                       ["-c", "from goszakup.bot import main"]]

    launcher.reset_mock()
    assert windows_run.prepare_python(tmp_path) == python
    assert launcher.call_count == 1
    assert launcher.call_args.args[0][1:] == ["-c", "from goszakup.bot import main"]

    launcher.reset_mock()
    requirements.write_text("structlog>=25.0\n", encoding="utf-8")
    assert windows_run.prepare_python(tmp_path) == python
    actions = [call.args[0][1:3] for call in launcher.call_args_list]
    assert actions == [["-c", "import pip"], ["-m", "pip"], ["-c", "from goszakup.bot import main"]]


def test_windows_launcher_stops_when_dependency_installation_fails(monkeypatch):
    monkeypatch.setattr(windows_run, "bot_instance", lambda _: nullcontext())
    monkeypatch.setattr(windows_run.sys, "argv", ["windows_run"])
    monkeypatch.setattr(windows_run, "update_main", lambda _: "5" * 40)
    prepare = Mock(side_effect=subprocess.CalledProcessError(1, ["pip", "install"]))
    monkeypatch.setattr(windows_run, "prepare_python", prepare)
    child = Mock()
    monkeypatch.setattr(windows_run.subprocess, "Popen", child)

    assert windows_run.main() == 1
    prepare.assert_called_once()
    child.assert_not_called()


def test_missing_bot_config_shows_setup_message_without_starting_bot(tmp_path, monkeypatch, capsys):
    from goszakup.bot import main as bot_main

    monkeypatch.setenv("BOT_CONFIG_PATH", str(tmp_path / "config.yaml"))
    launch = Mock()
    monkeypatch.setattr(bot_main, "_run_bot", launch)

    assert bot_main.main() == 1
    assert "Перенеси рабочий config.yaml" in capsys.readouterr().err
    launch.assert_not_called()


def test_windows_finds_git_with_a_stale_path(tmp_path, monkeypatch):
    monkeypatch.setattr(git_sync.os, "get_exec_path", lambda: [str(tmp_path / "stale")])
    install = tmp_path / "Git for Windows"
    executable = install / "cmd" / "git.exe"
    executable.parent.mkdir(parents=True)
    executable.touch()
    registry = SimpleNamespace(
        HKEY_LOCAL_MACHINE=1, HKEY_CURRENT_USER=2,
        OpenKey=Mock(return_value=nullcontext()),
        QueryValueEx=Mock(return_value=(str(install), 1)),
    )
    monkeypatch.setitem(sys.modules, "winreg", registry)
    assert git_sync._windows_git() == str(executable)
    registry.QueryValueEx.assert_called_once_with(None, "InstallPath")


def test_owner_chat_comes_from_one_explicit_recipient_or_environment(monkeypatch):
    monkeypatch.delenv("GOSZAKUP_REPORT_CHAT_ID", raising=False)
    assert _report_chat_id({"allowed_user_ids": [7]}) is None
    assert _report_chat_id({"report_chat_ids": [7, 8]}) is None
    assert _report_chat_id({"report_chat_ids": [7]}) == 7
    monkeypatch.setenv("GOSZAKUP_REPORT_CHAT_ID", "8")
    assert _report_chat_id({"report_chat_ids": [7]}) == 8
    monkeypatch.setenv("GOSZAKUP_REPORT_CHAT_ID", "-8")
    with pytest.raises(ValueError, match="personal Telegram chat ID"):
        _report_chat_id({})


def test_public_run_log_keeps_only_measurements(tmp_path):
    runtime = RuntimeLog(tmp_path, capture_reports=True)
    token = current_trace.set(BidTrace(f"#{TENDER}"))
    try:
        runtime.process(None, "info", {
            "event": "flow.timing.summary", "app_id": APPLICATION,
            "total_s": 24.8, "step": "public_ok", "password": "cabinet-password",
        })
        runtime.record_operation("GET", f"/files/download_file/{DOCUMENT}/opaque/?token=token-value",
                                 4.64, "200", None, f"#{TENDER}")
        runtime.record_operation("POST", f"/ru/application/ajax_public_application/{TENDER}/{APPLICATION}",
                                 3.3, "200", None, f"#{TENDER}")
        detailed, public = runtime.report_logs()
    finally:
        current_trace.reset(token)
        runtime.close()
    for private in (TENDER, APPLICATION, DOCUMENT):
        assert private in detailed and private not in public
    for secret in ("cabinet-password", "opaque", "token-value"):
        assert secret not in detailed and secret not in public
    assert "duration_s=4.640" in public and "route=/files/download_file" in public
    assert "total_s=24.8 step=public_ok" in public
    assert "route=/ru/application/ajax_public_application" in public


async def test_lifecycle_sends_owner_logs_and_pushes_public_checkpoints(tmp_path, monkeypatch):
    pushes = []
    pushed = asyncio.Event()
    loop = asyncio.get_running_loop()

    def publish(repo, files):
        assert repo == tmp_path
        pushes.append({name: data.decode() for name, data in files.items()})
        loop.call_soon_threadsafe(pushed.set)

    monkeypatch.setattr(run_reports, "publish_reports", publish)
    bot = SimpleNamespace(send_document=AsyncMock())
    runtime = SimpleNamespace(report_logs=lambda: ("detailed run", "public measurements"))
    reports = run_reports.RunReports(runtime, tmp_path, 1234, repo=tmp_path, revision="5" * 40)
    reports.start(SimpleNamespace(running=True, bot=bot))
    try:
        await asyncio.wait_for(pushed.wait(), 2)
        pushed.clear()
        bid_name = await reports.save_bid(BidTrace(), "public_ok")
        await asyncio.wait_for(pushed.wait(), 2)
        await reports.stop()
    finally:
        await reports.cancel()

    assert len(pushes) == 3
    assert "Бот запущен" in pushes[0][reports.filename]
    assert "Бот работает" in pushes[1][reports.filename]
    assert "Заявка подана" in pushes[1][bid_name]
    assert "Бот остановлен" in pushes[2][reports.filename]
    assert all("public measurements" in files[reports.filename] for files in pushes)
    assert all("detailed run" not in body for files in pushes for body in files.values())
    assert [(call.kwargs["chat_id"], call.kwargs["caption"].split(" ·")[0])
            for call in bot.send_document.await_args_list] == [
        (1234, "Бот запущен"), (1234, "Бот остановлен"),
    ]
    assert "detailed run" in (tmp_path / "runs" / reports.filename).read_text(encoding="utf-8")


async def test_bid_reports_survive_a_failed_push_and_restart(git_repos, tmp_path):
    repo, remote = git_repos
    runtime = SimpleNamespace(report_logs=lambda: ("local run", "public run"))
    reports = run_reports.RunReports(runtime, tmp_path, None, repo=repo)
    trace = BidTrace(f"#{TENDER}")
    trace.marks = [(0.0, "t0"), (16.0, "public_start"), (17.6, "public_done")]
    trace.events.append((16.0, 1.6, "POST",
                         f"/ru/application/ajax_public_application/{TENDER}/{APPLICATION}", "200/secret"))
    trace.events.append((9.2, 1.7, "POST",
                         f"/ru/application/show_doc/{TENDER}/{APPLICATION}/3334/77665544/2", "200"))
    trace.log("warning", "do not publish these raw notes")
    trace.logs.append((10.4, "info",
                       "local_signer.range.eligibility bytes=5646499 verdict=no_byte_ranges "
                       f"source=disabled fid={DOCUMENT} token=secret"))
    first = await reports.save_bid(trace, "public_ok")
    second = await reports.save_bid(trace, "public_error")
    assert first != second

    git(repo, "remote", "set-url", "origin", str(tmp_path / "offline.git"))
    assert not reports._save_and_push(b"local stopped", b"public stopped")
    assert (reports.pending / first).is_file()
    assert (reports.pending / second).is_file()

    git(repo, "remote", "set-url", "origin", str(remote))
    restarted = run_reports.RunReports(runtime, tmp_path, None, repo=repo)
    assert restarted._save_and_push(b"local restarted", b"public restarted")
    assert not list(restarted.pending.glob("*.txt"))
    one = git(remote, "show", f"main:reports/windows/{first}")
    two = git(remote, "show", f"main:reports/windows/{second}")
    assert "Заявка подана" in one and "17.60 с" in one
    assert "Портал отклонил заявку" in two
    assert "ajax_public_application" in one and "1.60 с" in one
    assert "/ru/application/show_doc/3334/2" in one
    assert "bytes=5646499 verdict=no_byte_ranges source=disabled" in one
    assert all(value not in one for value in (TENDER, APPLICATION, DOCUMENT, "77665544", "secret", "raw notes"))


async def test_managed_bid_automatically_sends_timing_only_to_owner(tmp_path, monkeypatch):
    trace = BidTrace(f"#{TENDER}")
    git_name = FILENAME.replace("run_", "bid_")
    monkeypatch.setitem(bid._BID_TRACES, 927, trace)
    monkeypatch.setattr(bid, "_LAST_REPORT", {})
    monkeypatch.setattr(bid, "runtime_summary", lambda: "Windows 11 · Chrome 123.0.1111.22")
    reports = SimpleNamespace(chat_id=4, save_bid=AsyncMock(return_value=git_name))
    ctx = SimpleNamespace(
        config=SimpleNamespace(raw={"telegram": {"report_chat_ids": [4, 5]}}, log_dir=tmp_path),
        run_reports=reports,
    )
    bot = SimpleNamespace(send_document=AsyncMock())
    await bid._send_timing_report(ctx, bot, 6, 927, TENDER, APPLICATION, result="public_ok")
    reports.save_bid.assert_awaited_once_with(trace, "public_ok", None)
    assert [call.kwargs["chat_id"] for call in bot.send_document.await_args_list] == [4]
    assert all(value in bid._LAST_REPORT[6][1] for value in (TENDER, APPLICATION, git_name))
    assert "Chrome 123.0.1111.22" in bid._LAST_REPORT[6][1]
    assert "Chrome" not in public_bid_report(trace, "public_ok")
