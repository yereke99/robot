"""Async pipeline for goszakup.gov.kz tender submission.

Architecture:
    nursign.py  -- WebSocket client to NCALayer/NURSign for ECP signing
    parser.py   -- HTML parsing to extract file_ids and form state
    state.py    -- persists application_id and last-completed-step
    client.py   -- async httpx wrapper for all 22 ajax_ endpoints
    pipeline.py -- orchestrator: pre-T0 warmup, T0 burst, signing loop, submit
"""

__version__ = "0.1.0"
