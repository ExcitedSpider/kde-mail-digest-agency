#!/usr/bin/env python3
"""Qt front end for the mail digest: a progress window with honest states.

Runs mail-digest.py, reads its ::stage:: lines straight off the pipe, and shows
what the triage is doing. Replaces the kdialog/D-Bus front end, which exposed no
way to set a window size.
"""
import os, re, signal, subprocess, sys
from pathlib import Path

from PyQt6.QtCore import Qt, QProcess, QProcessEnvironment, QTimer
from PyQt6.QtWidgets import (QApplication, QDialog, QDialogButtonBox, QLabel,
                             QMessageBox, QProgressBar, QVBoxLayout)

HOME = Path.home()
BIN = HOME / ".local/bin"
DIGEST = HOME / "mail-digest/latest.md"
LOG = HOME / ".local/state/mail-digest/run.log"
STAGE = re.compile(r"^::stage::(\d+)::(.*)$")
STEPS = 4
BUSY_STAGE = 3                      # the model call: duration genuinely unknown


class Progress(QDialog):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Mail triage")
        self.setFixedSize(880, 200)          # the one line kdialog could not offer
        self.stage, self.elapsed, self.text = 0, 0, "Starting mail triage…"

        self.label = QLabel(self.text)
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.label.setWordWrap(True)

        self.bar = QProgressBar()
        self.bar.setRange(0, STEPS)
        self.bar.setTextVisible(True)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        # Route through reject() so the Cancel button, the window's X and Esc
        # all take the same path. Connecting the button alone leaves the window
        # close orphaning the triage child with nothing left to open the viewer.
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 20)
        layout.addStretch(1)
        layout.addWidget(self.label)
        layout.addStretch(1)
        layout.addWidget(self.bar)
        layout.addWidget(buttons)

        env = QProcessEnvironment.systemEnvironment()
        env.insert("MAIL_DIGEST_PROGRESS", "1")
        env.insert("MAIL_DIGEST_NOTIFY", "0")

        self.proc = QProcess(self)
        self.proc.setProcessEnvironment(env)
        self.proc.setProcessChannelMode(QProcess.ProcessChannelMode.SeparateChannels)
        self.proc.readyReadStandardOutput.connect(self.on_output)
        self.proc.finished.connect(self.on_finished)
        self.proc.start(sys.executable, [str(BIN / "mail-digest.py")])

        self.ticker = QTimer(self)
        self.ticker.timeout.connect(self.tick)
        self.ticker.start(1000)

    # ---- progress ---------------------------------------------------------
    def on_output(self):
        data = bytes(self.proc.readAllStandardOutput()).decode("utf-8", "replace")
        for line in data.splitlines():
            m = STAGE.match(line.strip())
            if not m:
                continue
            stage, text = int(m.group(1)), m.group(2)
            if stage != self.stage:
                self.stage, self.elapsed = stage, 0
                # An unknown-length wait deserves a pulsating bar, not a fake number.
                self.bar.setRange(0, 0) if stage == BUSY_STAGE else self.bar.setRange(0, STEPS)
                if stage != BUSY_STAGE:
                    self.bar.setValue(stage)
            self.text = text
            self.refresh()

    def tick(self):
        self.elapsed += 1
        self.refresh()

    def refresh(self):
        suffix = f"  ({self.elapsed}s)" if self.stage == BUSY_STAGE else ""
        self.label.setText(self.text + suffix)

    # ---- outcomes ---------------------------------------------------------
    def reject(self):
        self.cancel()

    def cancel(self):
        if getattr(self, "_cancelled", False):
            return
        self._cancelled = True
        if self.proc.state() != QProcess.ProcessState.NotRunning:
            pid = int(self.proc.processId())
            subprocess.run(["pkill", "-P", str(pid)], check=False)
            self.proc.terminate()
            self.proc.waitForFinished(3000)
            self.proc.kill()
        notify("Mail triage cancelled", "Stopped before the digest was written.")
        QApplication.instance().exit(130)

    def on_finished(self, code, _status):
        self.ticker.stop()
        if code == 75:
            notify("Mail triage already running", "Another digest run is in progress.")
        elif code == 0:
            viewer = os.environ.get("MAIL_DIGEST_VIEWER") or "kate"
            QProcess.startDetached(viewer, [str(DIGEST)])
        else:
            tail = ""
            try:
                tail = "\n".join(LOG.read_text(errors="replace").splitlines()[-12:])
            except OSError:
                pass
            box = QMessageBox(QMessageBox.Icon.Critical, "Mail triage failed",
                              f"The triage run failed (exit {code}).")
            box.setDetailedText(tail or f"See {LOG}")
            box.exec()
        QApplication.instance().exit(0 if code in (0, 75) else code)


def notify(summary, body):
    subprocess.run(["notify-send", "--app-name=Mail triage", "--icon=mail-unread",
                    summary, body], check=False)


def main():
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    app = QApplication(sys.argv)
    app.setApplicationName("Mail triage")
    dialog = Progress()
    dialog.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
