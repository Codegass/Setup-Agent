# tests/test_venv_repair_ladder.py
"""Venv pip repair ladder — the Debian ensurepip-split rungs (live TVM
failure, session 20260713_014403_27874).

Live evidence reproduced: Ubuntu system python 3.12.3 WITHOUT the
python3.12-venv package (Debian splits ensurepip out), so the venv at
/workspace/tvm/.venv had only python symlinks — no pip, no activate. The old
ladder ran probe -> ensurepip -> recreate and STILL failed: ensurepip raised
'No module named ensurepip', the recreate used the SAME ensurepip-less system
python (pip-less venv again), and uv was 'not found' in the container.

The ladder now, straight-line, ONE attempt per rung:
  1. probe  `{venv}/bin/python -m pip --version`
  2. ensurepip
  3. recreate with the current interpreter (`python3 -m venv --clear`)
  4. NEW: apt-get install python3-venv python3-pip + versioned
          python3.<minor>-venv for the ACTIVE minor, then recreate
  5. NEW: install uv (curl | sh, PATH=$HOME/.local/bin) then `uv venv --seed`
  6. all exhausted -> ok=False, every rung named in the narration.

Scripted-orchestrator style (house pattern: tests/test_python_preflight.py,
tests/test_python_tool.py): first matching substring rule wins, every command
recorded.
"""

import sag.tools.internal.python_env as pe
from sag.tools.internal.python_env import ensure_venv_pip

VENV = "/workspace/tvm/.venv"


def ok(output=""):
    return {"success": True, "exit_code": 0, "output": output}


def fail(output="", exit_code=1):
    return {"success": False, "exit_code": exit_code, "output": output}


class LadderOrch:
    """Scriptable orchestrator modelling the venv repair ladder.

    ``pip_ok`` starts False (pip-less venv) and flips True the moment a rung
    that actually restores pip fires — modelling a working venv on re-probe.
    Each stage is one boolean/flag so a test can wire the exact TVM shape.
    """

    def __init__(
        self,
        *,
        python_output="Python 3.12.3",
        pip_ok=False,
        ensurepip_ok=False,          # Debian split: 'No module named ensurepip'
        recreate_restores_pip=False,  # same ensurepip-less python -> pip-less again
        apt_ok=False,                 # apt install python3-venv succeeds
        apt_recreate_restores_pip=False,  # post-apt recreate yields working pip
        uv_install_ok=False,          # curl | sh installs uv
        uv_venv_restores_pip=False,   # uv venv --seed yields working pip
    ):
        self.python_output = python_output
        self.pip_ok = pip_ok
        self.ensurepip_ok = ensurepip_ok
        self.recreate_restores_pip = recreate_restores_pip
        self.apt_ok = apt_ok
        self.apt_recreate_restores_pip = apt_recreate_restores_pip
        self.uv_installed = False
        self.uv_install_ok = uv_install_ok
        self.uv_venv_restores_pip = uv_venv_restores_pip
        self.commands = []
        self._saw_apt = False

    def execute_command(self, cmd, workdir=None):
        self.commands.append(cmd)

        if "python3 --version" in cmd:
            return ok(self.python_output)

        # pip probe reflects the CURRENT venv health.
        if "-m pip --version" in cmd:
            return ok("pip 25.0") if self.pip_ok else fail("No module named pip")

        # Rung 2: ensurepip. Debian split -> the module itself is missing.
        if "-m ensurepip" in cmd:
            if self.ensurepip_ok:
                self.pip_ok = True
                return ok()
            return fail("No module named ensurepip")

        # Rung 4a: apt install python3-venv / python3-pip.
        if "apt-get" in cmd and "python3" in cmd:
            self._saw_apt = self.apt_ok
            return ok() if self.apt_ok else fail("E: Unable to locate package")

        # uv install (curl | sh).
        if "astral.sh/uv/install.sh" in cmd:
            if self.uv_install_ok:
                self.uv_installed = True
                return ok()
            return fail("curl: command not found")

        # Rung 5: uv venv --seed.
        if "uv venv" in cmd:
            if self.uv_installed and self.uv_venv_restores_pip:
                self.pip_ok = True
                return ok()
            return fail("uv: not found")

        # venv (re)creation. `--clear` alone is the current-interpreter recreate
        # (rung 3); after a successful apt it is the post-apt recreate (rung 4b).
        if "-m venv" in cmd:
            if self._saw_apt and self.apt_recreate_restores_pip:
                self.pip_ok = True
                return ok()
            if not self._saw_apt and self.recreate_restores_pip:
                self.pip_ok = True
                return ok()
            return ok()  # command "succeeds" but the venv is still pip-less

        return ok()


def _index(orch, needle):
    return next(i for i, c in enumerate(orch.commands) if needle in c)


# ---------------------------------------------------------------------------
# The live TVM shape: ensurepip fails, recreate stays pip-less, apt rung wins.
# ---------------------------------------------------------------------------


def test_tvm_shape_apt_venv_rung_restores_pip():
    orch = LadderOrch(
        python_output="Python 3.12.3",
        ensurepip_ok=False,              # 'No module named ensurepip'
        recreate_restores_pip=False,     # same ensurepip-less python
        apt_ok=True,
        apt_recreate_restores_pip=True,  # python3.12-venv present -> pip works
    )
    result = ensure_venv_pip(orch, VENV)
    assert result["ok"] is True
    assert result["action"] == "apt-venv"
    # Straight-line ladder: probe -> ensurepip -> recreate -> apt -> recreate.
    ensurepip = _index(orch, "-m ensurepip")
    clear = _index(orch, "-m venv --clear")
    apt = _index(orch, "apt-get")
    assert ensurepip < clear < apt
    # The apt rung installs BOTH the generic and the versioned venv package for
    # the active minor (3.12 -> python3.12-venv).
    apt_cmd = orch.commands[apt]
    assert "python3-venv" in apt_cmd
    assert "python3-pip" in apt_cmd
    assert "python3.12-venv" in apt_cmd
    # Narration names the rungs tried.
    ladder = result.get("ladder") or []
    assert any("ensurepip" in r for r in ladder)
    assert any("recreate" in r for r in ladder)
    assert any("apt" in r for r in ladder)


def test_tvm_shape_apt_rung_is_a_single_attempt():
    orch = LadderOrch(
        ensurepip_ok=False,
        recreate_restores_pip=False,
        apt_ok=True,
        apt_recreate_restores_pip=True,
    )
    ensure_venv_pip(orch, VENV)
    apt_installs = [c for c in orch.commands if "apt-get" in c and "python3-venv" in c]
    assert len(apt_installs) == 1  # one attempt per rung, no loop


# ---------------------------------------------------------------------------
# Variant: apt ALSO fails but the uv rung wins.
# ---------------------------------------------------------------------------


def test_apt_fails_but_uv_rung_restores_pip():
    orch = LadderOrch(
        ensurepip_ok=False,
        recreate_restores_pip=False,
        apt_ok=False,                    # apt can't install python3-venv
        uv_install_ok=True,
        uv_venv_restores_pip=True,
    )
    result = ensure_venv_pip(orch, VENV)
    assert result["ok"] is True
    assert result["action"] == "uv"
    # uv was installed via curl|sh, then a seeded venv was created.
    uv_install = _index(orch, "astral.sh/uv/install.sh")
    uv_venv = _index(orch, "uv venv --seed")
    apt = _index(orch, "apt-get")
    assert apt < uv_install < uv_venv
    # PATH prepend so the just-installed uv is found.
    assert "$HOME/.local/bin" in orch.commands[uv_venv]
    ladder = result.get("ladder") or []
    assert any("apt" in r for r in ladder)
    assert any("uv" in r for r in ladder)


# ---------------------------------------------------------------------------
# Variant: EVERYTHING fails -> honest ok=False naming every rung.
# ---------------------------------------------------------------------------


def test_all_rungs_exhausted_is_honest_failure_naming_every_rung():
    orch = LadderOrch(
        ensurepip_ok=False,
        recreate_restores_pip=False,
        apt_ok=False,
        uv_install_ok=False,
        uv_venv_restores_pip=False,
    )
    result = ensure_venv_pip(orch, VENV)
    assert result["ok"] is False
    ladder = result.get("ladder") or []
    joined = " ".join(ladder).lower()
    for rung in ("ensurepip", "recreate", "apt", "uv"):
        assert rung in joined, f"exhausted narration must name the {rung} rung"


def test_all_rungs_attempted_once_each_no_loop():
    orch = LadderOrch(
        ensurepip_ok=False,
        recreate_restores_pip=False,
        apt_ok=False,
        uv_install_ok=False,
    )
    ensure_venv_pip(orch, VENV)
    assert sum("-m ensurepip" in c for c in orch.commands) == 1
    assert sum("apt-get" in c and "python3-venv" in c for c in orch.commands) == 1
    assert sum("astral.sh/uv/install.sh" in c for c in orch.commands) == 1
    assert sum("uv venv --seed" in c for c in orch.commands) == 1


# ---------------------------------------------------------------------------
# Regression: a healthy venv issues ZERO repair commands.
# ---------------------------------------------------------------------------


def test_healthy_venv_issues_no_repair_commands():
    orch = LadderOrch(pip_ok=True)
    result = ensure_venv_pip(orch, VENV)
    assert result["ok"] is True
    assert result["action"] is None
    assert not result.get("ladder")
    assert not any("-m ensurepip" in c for c in orch.commands)
    assert not any("apt-get" in c for c in orch.commands)
    assert not any("-m venv" in c for c in orch.commands)
    assert not any("uv venv" in c for c in orch.commands)
    # Exactly one probe, nothing else.
    assert orch.commands == [f"{VENV}/bin/python -m pip --version"]


# ---------------------------------------------------------------------------
# The earlier rungs still short-circuit: ensurepip alone wins when it can.
# ---------------------------------------------------------------------------


def test_ensurepip_alone_wins_without_reaching_apt_or_uv():
    orch = LadderOrch(ensurepip_ok=True)
    result = ensure_venv_pip(orch, VENV)
    assert result["ok"] is True
    assert result["action"] == "ensurepip"
    assert not any("apt-get" in c for c in orch.commands)
    assert not any("uv venv" in c for c in orch.commands)


def test_current_interpreter_recreate_wins_without_reaching_apt():
    orch = LadderOrch(ensurepip_ok=False, recreate_restores_pip=True)
    result = ensure_venv_pip(orch, VENV)
    assert result["ok"] is True
    assert result["action"] == "recreated"
    assert not any("apt-get" in c for c in orch.commands)
    assert not any("uv venv" in c for c in orch.commands)
