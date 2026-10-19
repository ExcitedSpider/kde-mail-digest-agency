#!/usr/bin/env python3
"""Orchestrate the mail digest: extract, triage, write, report.

Runs headless from a terminal or under mail-digest-qt.py, which reads the
::stage:: lines this emits on stdout. Exit codes: 0 success (including the
offline fallback), 75 another run holds the lock, 1 a real failure.
"""
import argparse, fcntl, json, os, re, socket, subprocess, sys
import datetime as dt
from pathlib import Path

from . import extract, fallback

HOME = Path.home()
OUT = HOME / "mail-digest"
STATE_DIR = HOME / ".local/state/mail-digest"
STATE = STATE_DIR / "seen.json"
LOCK = STATE_DIR / "lock"
LOG = STATE_DIR / "run.log"

WINDOW_DAYS = 10
MODEL = "claude-sonnet-5"
CLAUDE_TIMEOUT = 300
BUSY_EXIT = 75

HEADING = re.compile(r"^##\s+(.+?)[ \t]*\n", re.M)
STREAM_STAGE = 3
EMIT_EVERY = 0.4

PROMPT = """\
You are triaging one person's inbox. Stdin is JSON: recent messages from their
Thunderbird store, drawn from whichever accounts are configured there, typically
a mix of personal and work or university mail. Each message has "new": true if it
arrived since the last digest, false if an earlier digest already covered it.

Write a markdown digest for today, in this order, omitting any section that
would be empty:

## Needs action
Things only this person can resolve: replies owed to a human, payments due,
forms, confirmations, expiring access. One bullet each: what it is, who it is
from, the date, and the specific action. Lead with whatever has the nearest
deadline.

## Dated commitments
Appointments, meetings, reviews, bookings, deadlines and events in the next 14
days. Include these even when "new" is false — a standing commitment must keep
appearing until it has passed. Give the day, date and time.

Social and club events belong here: a ticket bought or a registration made is a
commitment, not an advertisement. If a message confirms a booking, ticket or
registration but never says when the event is, still list it, say the date is
not in the email, and name where to look — the ticket, the order, the organiser.

## Worth a glance
Only notices that would matter if this person had NOT done the thing
themselves: credential and access changes (a new 2FA factor, an added email or
phone, a regenerated access token, a password or recovery change), a charge or
transfer they may not recognise, or a sign-in from an unfamiliar device or
location. Say in a clause why it matters.

Treat as noise, never as a glance item: a device alarm, ring or location ping,
which only the account owner can trigger; one-time codes, magic links and
password-reset links; "account linked" mail that follows an ordinary social
sign-in; and receipts that plainly match a purchase elsewhere in this batch.
A notice is not worth surfacing just because it is security-shaped -- ask
whether anything would actually be wrong if it were not them.

## Noise
A single line counting what you ignored, by kind. No bullets.

Rules:
- Prioritise "new": true messages. Re-surface older ones only for dated
  commitments, or an unanswered request to this person that is going stale.
- Mail from a real person at a university or company outranks any automated
  notice.
- Automated channel-wide notifications (Teams group @mentions, newsletters,
  marketing, mass campus announcements) are noise unless they name a deadline
  or a dated event.
- A bulk message can still carry a real commitment. A club or society events
  round-up lists events this person may have tickets for: mine the dated events
  out of it into "Dated commitments", crediting the event and its host rather
  than the newsletter, and note "advertised, no booking seen" when nothing in
  the batch shows they signed up. The wrapper is noise; what it lists is not.
- Judge parcels and orders by their state, not by their sender. A parcel ready
  for collection, a failed or missed delivery, goods held pending a customs or
  duty payment, an order awaiting confirmation before it auto-completes, or a
  returns window about to close all go under "Needs action", with the deadline
  when one is stated and the collection point when one is named. Pure tracking
  telemetry is noise: dispatched, departed, in transit, cleared customs with
  nothing owed, out for delivery, and delivered with nothing left outstanding.
- Collapse every update about one parcel or order into a single item, judged on
  its latest state. Never count them individually.
- Each message carries a "flags" list. Every message flagged "deadline" must be
  accounted for somewhere: list it under "Needs action" or "Dated commitments",
  or dismiss it in the Noise line with a clause saying why nothing is needed
  (for instance a parcel since collected). Never drop a flagged message in
  silence.
- When in doubt, include. Money owed, access expiring, a window closing, or a
  person waiting on a reply all belong in "Needs action" even when the message
  reads like a template. A named individual writing from a company or
  university address and following up is a person awaiting an answer, not bulk
  mail. Noise is only for mail with nothing at stake.
- Never invent a date. If a message implies a deadline without stating one, say
  the date is unstated.
- Be terse. No preamble, no closing summary, no restating these instructions.
  Start with the first heading.
"""


def log(msg):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as fh:
        fh.write(f"{dt.datetime.now().astimezone().isoformat(timespec='seconds')} {msg}\n")


def stage(number, label):
    if os.environ.get("MAIL_DIGEST_PROGRESS") == "1":
        print(f"::stage::{number}::{label}", flush=True)


def notify(summary, body, urgent=False):
    if os.environ.get("MAIL_DIGEST_NOTIFY", "1") == "0":
        return
    cmd = ["notify-send", "--app-name=Mail triage", "--icon=mail-unread"]
    if urgent:
        cmd.append("--urgency=critical")
    subprocess.run(cmd + [summary, body], check=False)


def online():
    """A TCP handshake is enough; the HTTP reply would tell us nothing more."""
    if os.environ.get("MAIL_DIGEST_FORCE_OFFLINE") == "1":
        return False
    try:
        with socket.create_connection(("api.anthropic.com", 443), timeout=6):
            return True
    except OSError:
        return False


def human(n):
    return f"{n/1000:.1f}k" if n >= 1000 else str(n)


def payload(data):
    """The JSON the model sees. Kept separate so a failure can be saved as-is."""
    return json.dumps(data, indent=1, ensure_ascii=False)


def ask_claude(body_json, total):
    """Stream the model's answer, reporting the section being written."""
    import time
    cmd = ["claude", "-p", PROMPT, "--model", MODEL,
           "--output-format", "stream-json", "--include-partial-messages", "--verbose"]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True)
    proc.stdin.write(body_json)
    proc.stdin.close()

    chunks, final, section, last_emit = [], None, None, 0.0
    stage(STREAM_STAGE, "Sending to Claude…")
    deadline = time.monotonic() + CLAUDE_TIMEOUT

    for line in proc.stdout:
        if time.monotonic() > deadline:
            proc.kill()
            raise TimeoutError(f"claude exceeded {CLAUDE_TIMEOUT}s")
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = ev.get("type")
        if kind == "system" and ev.get("status") == "requesting":
            stage(STREAM_STAGE, "Waiting for Claude to start…")
        elif kind == "stream_event":
            inner = ev.get("event", {})
            if inner.get("type") == "content_block_delta":
                delta = inner.get("delta", {})
                if delta.get("type") != "text_delta":
                    continue
                chunks.append(delta.get("text", ""))
                found = HEADING.findall("".join(chunks[-24:]))
                if found and found[-1] != section:
                    section, last_emit = found[-1], 0.0
                now = time.monotonic()
                if now - last_emit >= EMIT_EVERY:
                    last_emit = now
                    chars = human(sum(len(c) for c in chunks))
                    stage(STREAM_STAGE,
                          f"Writing “{section}” — {chars} chars" if section
                          else f"Thinking — {chars} chars so far")
        elif kind == "assistant":
            blocks = ev.get("message", {}).get("content", [])
            joined = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
            if joined:
                final = joined
        elif kind == "result" and ev.get("is_error"):
            raise RuntimeError(ev.get("result") or "claude reported an error")

    proc.wait()
    body = final if final is not None else "".join(chunks)
    if proc.returncode != 0 or not body.strip():
        raise RuntimeError(f"claude exited {proc.returncode} with no usable text")
    return body.rstrip() + "\n"


def write_digest(path, today, new, total, body):
    header = f"# Mail digest — {today}\n\n*{new} new of {total} messages, {WINDOW_DAYS}-day window*\n\n"
    path.write_text(header + body)
    latest = OUT / "latest.md"
    if latest.is_symlink() or latest.exists():
        latest.unlink()
    latest.symlink_to(path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=WINDOW_DAYS)
    args = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    log("run start")

    # One run at a time: concurrent runs would race the digest and the seen state.
    lock_fh = LOCK.open("w")
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("another run is already in progress; exiting")
        notify("Mail triage already running", "Another digest run is in progress.")
        return BUSY_EXIT

    today = dt.date.today().isoformat()
    digest = OUT / f"{today}.md"

    stage(1, "Reading your mailboxes…")
    data = extract.collect(days=args.days, state=str(STATE))
    total, new = data["counts"]["total"], data["counts"]["new"]
    log(f"extracted total={total} new={new}")

    if total == 0:
        write_digest(digest, today, 0, 0,
                     f"No inbox mail in the last {args.days} days. Is Thunderbird syncing?\n")
        notify("Mail triage: nothing found",
               f"No mail in {args.days}d — check Thunderbird is running")
        return 0

    stage(2, f"Found {new} new of {total} messages — checking connection…")
    if not online():
        log("offline: using rules-only fallback")
        write_digest(digest, today, new, total, fallback.render(data))
        notify("Mail triage: offline", f"{new} new — keyword digest only, no network")
        return 0

    stage(STREAM_STAGE, f"Asking Claude to prioritise {total} messages…")
    try:
        body = ask_claude(payload(data), total)
    except (RuntimeError, TimeoutError, OSError) as exc:
        log(f"triage failed: {exc}; falling back to rules")
        # Keep the input that produced the failure, for inspection.
        (OUT / f"{today}-extract.json").write_text(payload(data))
        write_digest(digest, today, new, total,
                     f"> **Triage step failed** — rules-only digest below. Log: `{LOG}`\n\n"
                     + fallback.render(data))
        notify("Mail triage degraded", f"Claude step failed — keyword digest only, see {LOG}", True)
        return 0

    stage(4, "Writing the digest…")
    write_digest(digest, today, new, total, body)
    STATE.write_text(json.dumps({"message_ids": [m["id"] for m in data["messages"]][-4000:]}))

    headline = next((l.lstrip("- ").replace("**", "")
                     for l in body.splitlines() if l.startswith("- ")), "Digest ready")
    notify(f"Mail triage: {new} new", headline[:110])
    log("run end")
    return 0


if __name__ == "__main__":
    sys.exit(main())
