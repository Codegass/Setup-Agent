"""Tests for host session id uniqueness across concurrent setups."""

import os
import re

from sag.config.logger import generate_session_id


def test_session_ids_embed_pid_so_same_second_processes_never_collide():
    session_id = generate_session_id()

    assert re.fullmatch(r"\d{8}_\d{6}_\d+", session_id)
    assert session_id.endswith(f"_{os.getpid()}")
