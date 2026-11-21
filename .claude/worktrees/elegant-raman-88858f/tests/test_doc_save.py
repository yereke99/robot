"""Document save strategies.

serial (A, default): commit one save at a time on the shared session with a GET
barrier between — the proven, race-free order. The _SaveOp abstraction lets the
same prepared commit run on the shared session OR an isolated per-doc one.

parallel_sessions (B, experimental): each doc runs its full flow on its OWN login
and saves in parallel. These tests fake the prepare so they exercise only the
save orchestration (ordering, barriers, one-login-per-doc, fallback).
"""

from __future__ import annotations

import types

import pytest

from goszakup import flow
from goszakup.flow import _SaveOp, _save_serial


def _op(label: str, doc_id: int):
    async def run(c):
        c.events.append(("save", label))
    return _SaveOp(label, doc_id, run)


class _RecordingGz:
    def __init__(self):
        self.events: list[tuple] = []

    async def get_show_doc(self, ann, app_id, doc_id, suffix=""):
        self.events.append(("barrier", doc_id))
        return "<html></html>"


# --- serial (A) ------------------------------------------------------------------

async def test_save_serial_orders_save_then_barrier():
    gz = _RecordingGz()
    ops = [_op("doc_1253", 1253), _op("lot_3334_5_1", 3334)]
    await _save_serial(gz, "ann", "app", ops)
    assert gz.events == [
        ("save", "doc_1253"), ("barrier", 1253),
        ("save", "lot_3334_5_1"), ("barrier", 3334),
    ]


async def test_save_serial_barrier_failure_is_nonfatal():
    class _G(_RecordingGz):
        async def get_show_doc(self, *a, **k):
            raise RuntimeError("barrier boom")

    gz = _G()
    ran = []

    async def run(c):
        ran.append("x")

    await _save_serial(gz, "ann", "app", [_SaveOp("d", 1, run)])
    assert ran == ["x"]          # the save happened; the barrier error didn't bubble


# --- parallel_sessions (B) -------------------------------------------------------

class _FakeSib:
    def __init__(self):
        self.saved: list[int] = []
        self.barriers: list[int] = []
        self.logged_in = False
        self.logins = 0
        self.closed = False
        self._http = object()

    async def login(self, *, account_password, kalkan_signer):
        self.logged_in = True
        self.logins += 1
        return "CI"

    async def get_show_doc(self, ann, app_id, doc_id, suffix=""):
        self.barriers.append(doc_id)
        return ""

    async def aclose(self):
        self.closed = True


class _FakeMainGz:
    def __init__(self):
        self.siblings: list[_FakeSib] = []

    def clone_unauthenticated(self):
        s = _FakeSib()
        self.siblings.append(s)
        return s


def _fake_prepare(monkeypatch):
    async def fake_prepare(gz, ns, params, app_id, app_lot_map, beneficiary, base_url, spec):
        async def run(c):
            c.saved.append(spec.doc_id)
        return [_SaveOp(f"doc_{spec.doc_id}", spec.doc_id, run)]
    monkeypatch.setattr(flow, "_prepare_doc", fake_prepare)


def _params(**extra):
    p = dict(
        doc_plan=[flow.DocSpec(1253, "generate_sign"), flow.DocSpec(3334, "lot_sign")],
        auto_login_account_password="pw", signer=object(), announce_id="ann",
    )
    p.update(extra)
    return types.SimpleNamespace(**p)


async def test_parallel_sessions_spawns_when_no_pool(monkeypatch):
    """Empty pool -> a session is spawned per doc on the spot, used, and closed."""
    _fake_prepare(monkeypatch)
    gz = _FakeMainGz()
    ops = await flow._save_parallel_sessions(gz, _params(), "app", {7: 70}, None, "base", [])
    assert len(gz.siblings) == 2                          # one login per doc
    assert all(s.logged_in and s.closed for s in gz.siblings)
    assert all(s.barriers for s in gz.siblings)           # GET barrier after each save
    saved = sorted(d for s in gz.siblings for d in s.saved)
    assert saved == [1253, 3334]                          # each saved on its own session
    assert sorted(o.doc_id for o in ops) == [1253, 3334]


async def test_parallel_sessions_uses_prewarmed_pool(monkeypatch):
    """A pre-warmed pool is consumed as-is — no new logins, pool sessions NOT
    closed here (the AsyncExitStack owns them)."""
    _fake_prepare(monkeypatch)
    gz = _FakeMainGz()                                    # clone_unauthenticated would append here
    pool = [(_FakeSib(), object()), (_FakeSib(), object())]
    for sib, _ in pool:
        sib.logged_in = True                              # prewarmed
    ops = await flow._save_parallel_sessions(gz, _params(), "app", {7: 70}, None, "base", pool)
    assert gz.siblings == []                              # nothing spawned — pool reused
    assert all(not sib.closed for sib, _ in pool)         # stack closes them, not us
    saved = sorted(d for sib, _ in pool for d in sib.saved)
    assert saved == [1253, 3334]
    assert sorted(o.doc_id for o in ops) == [1253, 3334]


async def test_parallel_sessions_relogins_stale_pool_session(monkeypatch):
    """A pool session that expired during a long pre-T0 wait raises SessionExpired
    on first use -> we re-login it in place and retry, not crash."""
    state = {"raised": False}

    async def fake_prepare(gz, ns, params, app_id, app_lot_map, beneficiary, base_url, spec):
        if not state["raised"]:
            state["raised"] = True
            raise flow.SessionExpired("ci_session died mid-wait")
        async def run(c):
            c.saved.append(spec.doc_id)
        return [_SaveOp(f"doc_{spec.doc_id}", spec.doc_id, run)]
    monkeypatch.setattr(flow, "_prepare_doc", fake_prepare)

    sib = _FakeSib(); sib.logged_in = True
    pool = [(sib, object())]
    params = _params(doc_plan=[flow.DocSpec(1253, "generate_sign")])
    ops = await flow._save_parallel_sessions(_FakeMainGz(), params, "app", {7: 70}, None, "base", pool)
    assert sib.logins == 1                                # re-logged in once after the expiry
    assert sib.saved == [1253]                            # retry succeeded
    assert [o.doc_id for o in ops] == [1253]


async def test_prewarm_doc_pool_logs_in_one_per_doc(monkeypatch):
    gz = _FakeMainGz()

    class _Stack:
        def __init__(self): self.entered = []
        async def enter_async_context(self, c): self.entered.append(c); return c

    stack = _Stack()
    params = _params(doc_plan=[flow.DocSpec(1253, "generate_sign"),
                               flow.DocSpec(3359, "beneficiary"),
                               flow.DocSpec(3334, "lot_sign")])
    pool = await flow._prewarm_doc_pool(gz, params, stack)
    assert len(pool) == 3                                  # one session per doc
    assert all(sib.logged_in for sib, _ in pool)
    assert len(stack.entered) == 3                         # all registered for cleanup


async def test_process_documents_falls_back_to_serial_without_creds(monkeypatch):
    _fake_prepare(monkeypatch)

    class _G:
        def __init__(self):
            self.saved: list[int] = []

        def clone_unauthenticated(self):
            raise AssertionError("must not spawn a session without creds")

        async def get_show_doc(self, *a, **k):
            return ""

    # prepare appends to gz.saved via the op's run(c=gz)
    async def fake_prepare(gz, ns, params, app_id, app_lot_map, beneficiary, base_url, spec):
        async def run(c):
            c.saved.append(spec.doc_id)
        return [_SaveOp(f"doc_{spec.doc_id}", spec.doc_id, run)]
    monkeypatch.setattr(flow, "_prepare_doc", fake_prepare)

    gz = _G()
    params = types.SimpleNamespace(
        doc_plan=[flow.DocSpec(1253, "generate_sign")],
        auto_login_account_password=None,               # no creds -> can't do B
        signer=None, parallel_docs=True,
        doc_save_mode="parallel_sessions", announce_id="ann",
    )
    ops = await flow._process_documents(gz, None, params, "app", {7: 70}, None, "base")
    assert gz.saved == [1253]                            # ran serially on the main gz
    assert ops and ops[0].doc_id == 1253
