# kde-mail-digest-agency

Triage a local Thunderbird mailbox with Claude, from a KRunner launcher.

Press <kbd>Alt</kbd>+<kbd>Space</kbd>, type *Mail Digest*, and a progress window
reports what it is doing while Claude reads the last ten days of inbox mail. When
it finishes, the digest opens in your editor:

```markdown
## Needs action
- **Example Hosting** — service suspended for non-payment, invoice due 12 Oct, now overdue.
- **Marketplace order 1234** auto-completes soon — confirm receipt or file a refund.

## Dated commitments
- **Sat 24 Oct, 11:50 AM** — Dentist, with Dr Example.
- **Society info night** — ticket held, but the date is not in the email; check the order.

## Worth a glance
- Code host: an access token was regenerated — matters if it wasn't you.

## Noise
Ignored ~74: 17 parcel updates (all delivered), 15 newsletters, 8 chat mentions…
```

## Why it reads mbox files directly

Thunderbird keeps an offline copy of synced mail as plain mbox files. Reading
those needs no credentials and no IMAP session, which matters because modern mail
accounts use OAuth2 — a headless syncer would need the consent flow redone per
account, and a managed tenant may forbid it outright. If Thunderbird is already
syncing your mail, the data is already on disk.

The cost is that a digest reflects your last sync, not the live server.

## What it does

1. **`extract.py`** walks every `INBOX` under the profile's mail stores,
   reads the tail of each mbox, and emits recent non-bulk messages as JSON. It
   strips click-tracking URLs, invisible preheader padding and signed card
   payloads, which otherwise consume the whole text budget before any content.
2. It attaches deterministic flags — `deadline`, `ephemeral`, `automated-sender`,
   `routine-subject` — from a keyword pass.
3. **`triage.py`** sends that to Claude with a prompt that must account for
   every `deadline`-flagged message: list it, or dismiss it in the Noise line
   *with a reason*. Without that floor, items silently vanish between runs.
4. **`qtui.py`** shows progress by streaming the model's output, so the
   window names the section being written rather than counting seconds.

Messages already reported are remembered, so repeat runs surface what is new
while dated commitments keep reappearing until they pass.

## Deliberately on demand

There is no timer and no cron. A laptop that sleeps and loses connectivity runs
scheduled jobs at the wrong times or not at all, and silently. You trigger this
when you are at the machine and online.

## Layout

```
mail_digest/
  extract.py    mbox stores -> structured messages (importable and standalone)
  fallback.py   keyword-only digest, for offline or a failed triage
  triage.py     lock, extract, ask Claude, write the digest
  qtui.py       the progress window
install.py      copies the package, writes launchers and the desktop entry
```

`triage.py` imports its siblings rather than shelling out to them. The one
remaining child process is the Qt window's: it runs the triage as a subprocess
so that cancelling can kill an in-flight `claude` call.

## Requirements

- KDE Plasma (the launcher is a `.desktop` entry; KRunner finds it)
- Thunderbird with offline copies of the folders you care about
- Python 3.11+, `python3-pyqt6` for the progress window
- [Claude Code](https://claude.com/claude-code) on `PATH`, authenticated

Without `claude`, or with no network, it still produces a keyword-only digest and
says so at the top.

## Install

```bash
git clone git@github.com:ExcitedSpider/kde-mail-digest-agency.git
cd kde-mail-digest-agency
./install.py
```

The package goes to `~/.local/share/mail-digest`, two launchers (`mail-digest`
and `mail-digest-gui`) to `~/.local/bin`, and the desktop entry to
`~/.local/share/applications`. `./install.py --uninstall` removes all three and
leaves your digests alone.

It copies rather than using pip, because distributions increasingly mark the
system interpreter as externally managed (PEP 668), where `pip install --user`
fails without override flags.

## Use

Launch *Mail Digest* from KRunner, or run it headless:

```bash
mail-digest                      # writes ~/mail-digest/YYYY-MM-DD.md
mail-digest-gui                  # the same, behind the progress window
```

Each module also runs standalone, which is useful when adjusting the extractor:

```bash
python3 -m mail_digest.extract --days 10            # JSON on stdout
python3 -m mail_digest.extract | python3 -m mail_digest.fallback
```

| Variable | Effect |
| --- | --- |
| `MAIL_DIGEST_VIEWER` | editor to open the digest (default `kate`) |
| `MAIL_DIGEST_NOTIFY=0` | suppress desktop notifications |
| `MAIL_DIGEST_PROGRESS=1` | emit `::stage::` lines on stdout |
| `MAIL_DIGEST_FORCE_OFFLINE=1` | skip Claude, use keyword rules |

Digests land in `~/mail-digest/`, with `latest.md` symlinked to the newest. State
and logs live in `~/.local/state/mail-digest/`; delete `seen.json` to treat every
message as new again.

## Tuning

- **Window**: `WINDOW_DAYS` in `mail_digest/triage.py`.
- **What counts as a deadline**: the `DEADLINE` regex in `mail_digest/extract.py`. This
  is the honest lever for a digest that is too long — widening the prompt's
  wording tends to make inclusion drift between runs instead.
- **What the digest contains**: the `PROMPT` constant. The sections, the recall
  floor, and the rules for parcels and security notices all live there.

## Privacy

Your mail is read locally and sent to Claude's API for triage; nothing else
leaves the machine. Digests are plain files in your home directory. The repo
contains no mail — but note that a digest, by design, summarises it, so treat
`~/mail-digest/` as you would the mailbox itself.

## Known limitations

- Only `INBOX` folders are scanned; filtered mail in subfolders is not seen.
- Teams and other adaptive-card notifications carry no readable text, so they are
  judged on their subject alone.
- The commitment window is 14 days, so something further out drops off the digest
  until it approaches.
