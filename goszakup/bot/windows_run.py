"""Windows launcher used by the batch files, shortcuts and service."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from .git_sync import GitSyncError, update_main
from .instance_lock import BotAlreadyRunning, bot_instance


def main() -> int:
    parser = argparse.ArgumentParser(description="Update origin/main and start Atagos.")
    parser.add_argument("--update-only", action="store_true")
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[2]
    try:
        with bot_instance(f"windows-launch:{os.path.normcase(str(repo))}"):
            revision = update_main(repo)
            print(f"origin/main updated: {revision[:12]}", flush=True)
            if args.update_only:
                return 0
            env = {
                **os.environ,
                "GOSZAKUP_MANAGED_START": "1",
                "GOSZAKUP_RUN_REVISION": revision,
            }
            with subprocess.Popen([sys.executable, "-m", "goszakup.bot.main"], cwd=repo, env=env) as bot:
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
