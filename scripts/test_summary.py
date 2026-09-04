#!/usr/bin/env python3
"""Quick manual test for summarize_discussion_notes — no shell-quoting
headaches. Put the client's raw answer (any length, any punctuation,
multiple lines) into a plain text file, then run:

    python scripts/test_summary.py path/to/answer.txt "Client Name"

If no file is given, it reads from scripts/sample_client_answer.txt.
The summary is also written to scripts/last_summary_output.txt — open
that in a text editor rather than reading it off the terminal if
anything looks like it's missing spaces or wrapped oddly; long terminal
lines can get mangled on copy/paste in a way the actual text never was.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.summarizer import summarize_discussion_notes

if __name__ == "__main__":
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / "sample_client_answer.txt"
    client_name = sys.argv[2] if len(sys.argv) > 2 else "Test Client"

    if not path.exists():
        print(f"No file at {path} — create it with the client's raw answer and try again.")
        sys.exit(1)

    raw_text = path.read_text()
    print(f"--- Raw answer ({len(raw_text)} chars) ---")
    print(raw_text)

    summary = summarize_discussion_notes(client_name, raw_text)

    print(f"\n--- Groq summary (what the manager would see) ---")
    print(summary)

    out_path = Path(__file__).parent / "last_summary_output.txt"
    out_path.write_text(summary)
    print(f"\n(also written to {out_path} — open that file directly if anything above looks like it's missing spaces)")
