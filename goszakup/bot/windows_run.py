"""Windows launcher used by the batch files, shortcuts and service."""

from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
from pathlib import Path

from .git_sync import GitSyncError, update_main
from .instance_lock import BotAlreadyRunning, bot_instance


def prepare_python(repo: Path) -> Path:
    """Install the launch checkout's dependencies in its own environment."""
    venv = repo / ".venv"
    python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not python.is_file():
        print("Создаю окружение .venv.", flush=True)
        subprocess.run([sys.executable, "-m", "venv", str(venv)], cwd=repo, check=True)

    requirements = repo / "requirements.txt"
    digest = hashlib.sha256(requirements.read_bytes()).hexdigest().encode("ascii")
    installed = venv / "atagos-requirements.sha256"
    smoke_check = [str(python), "-c", "from goszakup.bot import main"]
    if installed.is_file() and installed.read_bytes() == digest:
        result = subprocess.run(
            smoke_check, cwd=repo, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if result.returncode == 0:
            return python

    print("Устанавливаю зависимости из requirements.txt.", flush=True)
    pip_ready = subprocess.run(
        [str(python), "-c", "import pip"], cwd=repo,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    if pip_ready.returncode:
        subprocess.run([str(python), "-m", "ensurepip"], cwd=repo, check=True)
    subprocess.run(
        [str(python), "-m", "pip", "install", "--no-input", "--disable-pip-version-check",
         "-r", str(requirements)],
        cwd=repo, check=True,
    )
    subprocess.run(smoke_check, cwd=repo, check=True)
    installed.write_bytes(digest)
    return python


def main() -> int:
    parser = argparse.ArgumentParser(description="Update origin/main and start Atagos.")
    parser.add_argument("--update-only", action="store_true", help="update code and dependencies, then exit")
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[2]
    try:
        with bot_instance(f"windows-launch:{os.path.normcase(str(repo))}"):
            revision = update_main(repo)
            print(f"origin/main updated: {revision[:12]}", flush=True)
            try:
                python = prepare_python(repo)
            except (OSError, subprocess.CalledProcessError) as exc:
                print(f"Не удалось подготовить .venv. Проверь сообщения выше ({type(exc).__name__}).",
                      file=sys.stderr)
                return 1
            if args.update_only:
                return 0
            env = {
                **os.environ,
                "GOSZAKUP_MANAGED_START": "1",
                "GOSZAKUP_RUN_REVISION": revision,
            }
            print(f"Бот запускается через {python}", flush=True)
            with subprocess.Popen([str(python), "-m", "goszakup.bot.main"], cwd=repo, env=env) as bot:
                try:
                    return bot.wait()
                except KeyboardInterrupt:
                    # Parent and child share a console. Give the bot time to
                    # checkpoint its report after they receive Ctrl+C.
                    return bot.wait()
    except BotAlreadyRunning:
        print("Бот уже запущен из этой папки.", file=sys.stderr)
    except GitSyncError as exc:
        print(f"Не удалось обновить origin/main. Запуск остановлен ({exc}).", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
