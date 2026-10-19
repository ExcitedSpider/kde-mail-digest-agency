#!/usr/bin/env python3
"""Offline digest: turn mail-extract.py JSON into markdown using rules only.

Used when there is no network for the Claude triage step. No judgement, just
keyword flags and grouping -- enough to see whether anything looks urgent.
"""
import json, sys, collections

def render(data):
    """Return a markdown digest built from flags alone. No model involved."""
    msgs = data["messages"]
    flagged, people, rest = [], [], []
    for m in msgs:
        f = m.get("flags", [])
        if "deadline" in f:
            flagged.append(m)
        elif not ({"automated-sender", "routine-subject", "ephemeral"} & set(f)):
            people.append(m)
        else:
            rest.append(m)

    out = []
    out.append("> **Offline digest** — no network, so this is keyword matching only, "
               "not a real triage. Re-run when connected for a prioritised version.\n")

    if flagged:
        out.append("## Mentions a deadline or payment\n")
        for m in flagged:
            tag = "" if m["new"] else " *(seen before)*"
            out.append(f'- **{m["date"]}** — {m["subject"]}{tag}  \n'
                       f'  from {m["from"]}')
        out.append("")

    if people:
        out.append("## From a person, not a robot\n")
        for m in people:
            tag = "" if m["new"] else " *(seen before)*"
            out.append(f'- **{m["date"]}** — {m["subject"]}{tag}  \n  from {m["from"]}')
        out.append("")

    if rest:
        by_account = collections.Counter(m["account"] for m in rest)
        out.append("## Everything else\n")
        out.append(f'{len(rest)} automated messages: '
                   + ", ".join(f"{n} in {a}" for a, n in by_account.most_common()) + "\n")
        for m in rest[-15:]:
            out.append(f'- {m["date"]} — {m["subject"][:90]}')
    return "\n".join(out) + "\n"


def main():
    print(render(json.load(sys.stdin)), end="")


if __name__ == "__main__":
    main()
