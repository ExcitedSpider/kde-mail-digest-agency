#!/usr/bin/env python3
"""Install the mail digest tools for the current user.

Copies the scripts into ~/.local/bin, writes the KRunner desktop entry with
absolute paths filled in, and refreshes KDE's application cache. Idempotent:
run it again after pulling to update an existing install.
"""
import argparse, os, shutil, subprocess, sys
from pathlib import Path

SCRIPTS = ["mail-extract.py", "mail-digest.py", "mail-digest-qt.py", "mail-fallback.py"]
HERE = Path(__file__).resolve().parent


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bindir", type=Path, default=Path.home() / ".local/bin")
    ap.add_argument("--uninstall", action="store_true")
    args = ap.parse_args()

    apps = Path.home() / ".local/share/applications"
    desktop = apps / "mail-digest.desktop"

    if args.uninstall:
        for name in SCRIPTS:
            (args.bindir / name).unlink(missing_ok=True)
        desktop.unlink(missing_ok=True)
        refresh()
        print("removed scripts and desktop entry (digests and state left alone)")
        return 0

    missing = [m for m in ("PyQt6",) if not importable(m)]
    if missing:
        print(f"warning: {', '.join(missing)} not importable -- the progress window "
              f"needs it (Fedora: sudo dnf install python3-pyqt6)", file=sys.stderr)
    if not shutil.which("claude"):
        print("warning: 'claude' not on PATH -- triage will fall back to keyword rules",
              file=sys.stderr)

    args.bindir.mkdir(parents=True, exist_ok=True)
    apps.mkdir(parents=True, exist_ok=True)
    for name in SCRIPTS:
        target = args.bindir / name
        shutil.copy2(HERE / "bin" / name, target)
        target.chmod(0o755)

    template = (HERE / "mail-digest.desktop.in").read_text()
    desktop.write_text(template
                       .replace("@PYTHON@", sys.executable)
                       .replace("@BINDIR@", str(args.bindir)))
    refresh()
    print(f"installed {len(SCRIPTS)} scripts to {args.bindir}")
    print("launch it from KRunner (Alt+Space) as \"Mail Digest\"")
    return 0


def importable(mod):
    try:
        __import__(mod)
        return True
    except ImportError:
        return False


def refresh():
    for tool in ("kbuildsycoca6", "kbuildsycoca5"):
        if shutil.which(tool):
            subprocess.run([tool, "--noincremental"], check=False,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if shutil.which("update-desktop-database"):
        subprocess.run(["update-desktop-database",
                        str(Path.home() / ".local/share/applications")], check=False)


if __name__ == "__main__":
    sys.exit(main())
