"""Triage a local Thunderbird mailbox with Claude.

extract  -- Thunderbird mbox stores to structured messages
fallback -- keyword-only digest, used offline or when triage fails
triage   -- orchestration: lock, extract, ask Claude, write the digest
qtui     -- the progress window
"""
__version__ = "1.0.0"
