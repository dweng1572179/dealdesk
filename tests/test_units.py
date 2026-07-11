"""Runs the per-module self-checks (each module's demo()) in one shot, on temp DBs.
Run from the dealdesk/ dir:  python -m tests.test_units"""
import os
import tempfile

os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "units.db")
# force the keyless / no-inbox paths regardless of any local .env — the module demos
# exercise the rules/template fallbacks, not the live API. Must be set before importing
# app.config (the settings singleton reads env at import time).
os.environ["ANTHROPIC_API_KEY"] = ""
os.environ["EMAIL_USER"] = os.environ["EMAIL_PASSWORD"] = ""

from app import ai, budget, csvimport, db, docparse, inbox, matching, seed, underwriting  # noqa: E402


def run() -> None:
    ai.demo()
    matching.demo()
    underwriting.demo()
    docparse.demo()
    inbox.demo()
    budget.demo()
    csvimport.demo()
    db.demo()
    seed.demo()
    print("test_units OK")


if __name__ == "__main__":
    run()
