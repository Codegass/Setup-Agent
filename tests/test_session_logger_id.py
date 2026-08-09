"""Tests for host session id uniqueness across concurrent setups."""

import os
import re

from sag.config.logger import generate_session_id


def test_session_ids_embed_nonce_and_pid_for_process_and_command_uniqueness():
    first = generate_session_id()
    second = generate_session_id()

    assert re.fullmatch(r"\d{8}_\d{6}_\d{6}_[0-9a-f]{12}_\d+", first)
    assert first.endswith(f"_{os.getpid()}")
    assert first != second
