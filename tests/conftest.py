"""Keep the test suite out of the real draft log.

`state.DRAFT_LOG` is a relative path, so anything that ingests picks — including
every poller test that boots against `samples/` — appends to whichever
`draft_log.jsonl` sits in the working directory. Run the suite from the repo root
and that is the user's actual draft record, which is gitignored and therefore the
only copy. Redirect it for the whole session instead.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import state  # noqa: E402


@pytest.fixture(autouse=True, scope="session")
def _isolate_draft_log(tmp_path_factory):
    original = state.DRAFT_LOG
    state.DRAFT_LOG = tmp_path_factory.mktemp("draftlog") / "draft_log.jsonl"
    yield
    state.DRAFT_LOG = original
