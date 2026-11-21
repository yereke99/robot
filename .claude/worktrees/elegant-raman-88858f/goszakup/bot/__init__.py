"""Telegram bot wrapping the goszakup fast bidder.

Entry point: `python -m goszakup.bot.main`.

Single-tenant (one BIN, one .p12), multi-user (whitelist of Telegram
user_ids). One bid in flight per bot process — second user sees a queue.
"""
