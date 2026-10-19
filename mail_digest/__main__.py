"""python -m mail_digest -- run a triage and write today's digest."""
import sys

from .triage import main

if __name__ == "__main__":
    sys.exit(main())
