"""Persisted submission state for --resume.

After every successful step we write {step, application_id, signed_files, ...}
to ~/.goszakup/state/{announce_id}.json with an atomic rename. If the script
dies mid-flight, the next run with --resume skips already-completed steps.

Schema is intentionally flat -- no migrations, just additive fields. Unknown
fields are tolerated on read so old state files keep working.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

DEFAULT_STATE_DIR = Path("~/.goszakup/state").expanduser()


@dataclass
class SubmissionState:
    announce_id: str
    application_id: Optional[str] = None
    step: str = "init"  # init|created|lots_added|lots_confirmed|docs_signed|priced|signed|public_ok|public_error
    signed_file_ids: list[int] = field(default_factory=list)
    error: Optional[str] = None
    timestamps: dict[str, float] = field(default_factory=dict)


class StateStore:
    def __init__(self, state_dir: Path = DEFAULT_STATE_DIR, *, namespace: str = ""):
        """`namespace` puts state under a subdirectory (e.g. per Telegram
        user_id) so concurrent bot users don't clobber each other's drafts.
        """
        if namespace:
            state_dir = state_dir / namespace
        self.state_dir = state_dir
        self.state_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, announce_id: str) -> Path:
        return self.state_dir / f"{announce_id}.json"

    def load(self, announce_id: str) -> SubmissionState:
        p = self._path(announce_id)
        if not p.exists():
            return SubmissionState(announce_id=announce_id)
        with p.open() as f:
            data = json.load(f)
        # Drop unknown fields so adding new fields stays backwards-compatible.
        known = {f.name for f in SubmissionState.__dataclass_fields__.values()}
        return SubmissionState(**{k: v for k, v in data.items() if k in known})

    def save(self, state: SubmissionState) -> None:
        p = self._path(state.announce_id)
        # Atomic write: tmp file in same dir, then rename. Survives crash mid-write.
        fd, tmp = tempfile.mkstemp(dir=self.state_dir, prefix=".tmp-", suffix=".json")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(asdict(state), f, indent=2, ensure_ascii=False)
            os.replace(tmp, p)
        except Exception:
            os.unlink(tmp)
            raise

    def clear(self, announce_id: str) -> None:
        p = self._path(announce_id)
        if p.exists():
            p.unlink()
