#!/usr/bin/env python3
"""Extract recent, non-bulk messages from a Thunderbird offline mail store.

Reads mbox files directly (read-only; safe while Thunderbird is running) and
emits compact JSON on stdout. Knows nothing about Claude -- the triage step
consumes its output. Portable to any host with a Thunderbird/mbox store.
"""
import argparse, configparser, email, email.header, email.utils, datetime, json
import html, os, re, sys
from email.parser import BytesParser

SEPARATOR = re.compile(rb"^From - \w{3} \w{3}")
MSG_CAP = 40960          # bytes kept per message: headers + enough body for a snippet
DEFAULT_TAIL = 80        # MiB read from the end of each mbox
# Deterministic flags travel with each message so the triage step has a recall
# floor: anything flagged must be accounted for, not silently dropped. Shared
# with mail-fallback.py, which renders the offline digest from the same flags.
DEADLINE = re.compile(
    r"\b(deadline|due date|overdue|action required|expir(?:e|es|ed|ing|y)|"
    r"respond by|reply by|rsvp|submit by|closes? on|closing date|suspend(?:ed)?|"
    r"payment (?:due|required)|final notice|confirm(?:ation)? required|unpaid|"
    r"awaiting confirmation|ready for collection|delivery issue)\b", re.I)
EPHEMERAL = re.compile(
    r"\b(verification|security|confirmation|access|login|one[- ]?time)\s+code\b"
    r"|\b(otp|passcode|secure link|magic link|sign[- ]?in link|reset your password)\b",
    re.I)
AUTOMATED_SENDER = re.compile(
    r"\b(no[-_.]?reply|do[-_.]?not[-_.]?reply|noreply|notifications?|automated|"
    r"mailer|bounce|support|service|transaction|marketing|newsletter)\b", re.I)
ROUTINE_SUBJECT = re.compile(
    r"\b(newsletter|weekly|unsubscribe|shipped|delivered|dispatch|tracking|"
    r"mentioned|statement|receipt|invoice)\b", re.I)

INVISIBLE = {c: " " for c in
             [0x00ad, 0x034f, 0x2060, 0xfeff] + list(range(0x200b, 0x2010))}


def find_profile(explicit=None):
    if explicit:
        return explicit
    root = os.path.expanduser("~/.thunderbird")
    ini = os.path.join(root, "profiles.ini")
    if not os.path.exists(ini):
        sys.exit(f"no Thunderbird profiles.ini under {root}")
    cp = configparser.ConfigParser()
    cp.read(ini)
    best, best_size = None, -1
    for sec in cp.sections():
        if not sec.startswith("Profile"):
            continue
        path = cp[sec].get("Path")
        if not path:
            continue
        full = path if os.path.isabs(path) else os.path.join(root, path)
        # Pick the profile that actually holds mail, not merely the default flag.
        size = sum(
            os.path.getsize(os.path.join(dp, f))
            for store in ("ImapMail", "Mail")
            for dp, _, fs in os.walk(os.path.join(full, store))
            for f in fs
        ) if os.path.isdir(full) else 0
        if size > best_size:
            best, best_size = full, size
    if not best:
        sys.exit("no usable Thunderbird profile found")
    return best


def find_inboxes(profile):
    """Every mbox named INBOX under the IMAP/local stores, plus local Inbox."""
    out = []
    for store in ("ImapMail", "Mail"):
        base = os.path.join(profile, store)
        for dp, _, files in os.walk(base):
            for f in files:
                if f in ("INBOX", "Inbox"):
                    out.append(os.path.join(dp, f))
    return sorted(out)


def account_tag(path, profile):
    rel = os.path.relpath(path, profile)
    parts = rel.split(os.sep)
    return parts[1] if len(parts) > 2 else parts[0]


def iter_messages(path, tail_bytes):
    """Yield truncated raw messages from the tail of an mbox, oldest first."""
    size = os.path.getsize(path)
    with open(path, "rb") as fh:
        if size > tail_bytes:
            fh.seek(size - tail_bytes)
            fh.readline()          # discard the partial line we landed mid-way through
        buf, kept, started = [], 0, False
        for line in fh:
            if SEPARATOR.match(line):
                if started:
                    yield b"".join(buf)
                buf, kept, started = [], 0, True
                continue
            if started and kept < MSG_CAP:
                buf.append(line)
                kept += len(line)
        if started and buf:
            yield b"".join(buf)


def decode_header(value):
    if not value:
        return ""
    try:
        return "".join(
            part.decode(charset or "utf-8", "replace") if isinstance(part, bytes) else part
            for part, charset in email.header.decode_header(str(value))
        )
    except Exception:
        return str(value)


def clean_text(body, is_html):
    """Reduce a mail part to readable prose: no markup, no stylesheet noise."""
    if is_html or re.search(r"<(table|div|td|br|p|span|a)\b", body, re.I):
        body = re.sub(r"(?is)<(script|style|head)\b.*?</\1>", " ", body)
        body = re.sub(r"(?s)<!--.*?-->", " ", body)
        body = re.sub(r"<[^>]+>", " ", body)
    # Inline CSS survives de-tagging as "selector { prop: value }" rubble.
    body = re.sub(r"(?s)[.#@]?[-\w\[\]=\"':\s,>*()]{0,80}\{[^{}]{0,400}\}", " ", body)
    body = html.unescape(body)
    # Click-tracking URLs are often most of a transactional mail's text and
    # would otherwise consume the whole snippet budget before any real content.
    body = re.sub(r"<?\b(?:https?://|mailto:)[^\s<>\"\']{4,}>?", " ", body)
    # Senders pad preheaders with invisible characters to control inbox previews.
    body = body.translate(INVISIBLE)
    # Teams/Outlook actionable cards carry a signed JWT payload, never prose.
    body = re.sub(r"[A-Za-z0-9+/_=-]{200,}", " ", body)
    return re.sub(r"\s+", " ", body).strip()


def snippet(msg, limit=700):
    """Best readable text from a message: text/plain preferred, HTML as fallback."""
    plain, fallback = "", ""
    try:
        for part in msg.walk():
            if part.get_content_maintype() != "text":
                continue
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            body = payload.decode(part.get_content_charset() or "utf-8", "replace")
            is_html = part.get_content_subtype() == "html"
            text = clean_text(body, is_html)
            if len(text) < 12 or "SignedAdaptiveCard" in text:
                continue
            if not is_html:
                plain = text
                break
            fallback = fallback or text
    except Exception:
        pass
    return (plain or fallback)[:limit]


def flags_for(sender, subject, body):
    found = []
    if EPHEMERAL.search(subject):
        found.append("ephemeral")              # a code that expires is never actionable later
    elif DEADLINE.search(f"{subject} {body}"):
        found.append("deadline")
    if AUTOMATED_SENDER.search(sender):
        found.append("automated-sender")
    if ROUTINE_SUBJECT.search(subject):
        found.append("routine-subject")
    return found


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7, help="look-back window")
    ap.add_argument("--tail-mib", type=int, default=DEFAULT_TAIL,
                    help="bytes read from the end of each mbox")
    ap.add_argument("--profile", help="override profile directory")
    ap.add_argument("--state", help="JSON file of already-reported Message-IDs")
    ap.add_argument("--include-bulk", action="store_true",
                    help="keep List-Id/List-Unsubscribe mail (dropped by default)")
    args = ap.parse_args()

    profile = find_profile(args.profile)
    inboxes = find_inboxes(profile)
    if not inboxes:
        sys.exit(f"no INBOX mbox files under {profile}")

    seen = set()
    if args.state and os.path.exists(args.state):
        try:
            seen = set(json.load(open(args.state)).get("message_ids", []))
        except Exception:
            seen = set()

    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=args.days)
    parser = BytesParser()
    messages = []

    for path in inboxes:
        tag = account_tag(path, profile)
        for raw in iter_messages(path, args.tail_mib * 1024 * 1024):
            try:
                msg = parser.parsebytes(raw)
            except Exception:
                continue
            raw_date = msg.get("Date")
            if not raw_date:
                continue
            try:
                when = email.utils.parsedate_to_datetime(raw_date)
            except Exception:
                continue
            if when is None:
                continue
            if when.tzinfo is None:
                when = when.replace(tzinfo=datetime.timezone.utc)
            if when < cutoff:
                continue
            bulk = bool(msg.get("List-Id") or msg.get("List-Unsubscribe"))
            if bulk and not args.include_bulk:
                continue
            mid = (msg.get("Message-ID") or "").strip() or f"{tag}:{raw_date}:{msg.get('Subject')}"
            sender = decode_header(msg.get("From"))[:120]
            subject = decode_header(msg.get("Subject"))[:200]
            body = snippet(msg)
            messages.append({
                "id": mid,
                "flags": flags_for(sender, subject, body),
                "account": tag,
                "date": when.astimezone().strftime("%Y-%m-%d %H:%M"),
                "ts": when.timestamp(),
                "from": sender,
                "subject": subject,
                "bulk": bulk,
                "new": mid not in seen,
                "snippet": body,
            })

    messages.sort(key=lambda m: m["ts"])
    for m in messages:
        m.pop("ts")

    json.dump({
        "generated": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "today": datetime.date.today().isoformat(),
        "profile": profile,
        "accounts": sorted({m["account"] for m in messages}),
        "window_days": args.days,
        "counts": {"total": len(messages), "new": sum(1 for m in messages if m["new"])},
        "messages": messages,
    }, sys.stdout, indent=1, ensure_ascii=False)


if __name__ == "__main__":
    main()
