import subprocess
import sys
import venv
from pathlib import Path


def test_wheel_installs_and_cli_loads(tmp_path):
    repo_root = Path(__file__).resolve().parents[1]
    dist_dir = tmp_path / "dist"

    subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(dist_dir)],
        cwd=repo_root,
        check=True,
    )

    wheels = list(dist_dir.glob("*.whl"))
    assert len(wheels) == 1

    venv_dir = tmp_path / "venv"
    venv.EnvBuilder(with_pip=True).create(venv_dir)
    python = venv_dir / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    sag_bin = venv_dir / ("Scripts/sag.exe" if sys.platform == "win32" else "bin/sag")

    subprocess.run([str(python), "-m", "pip", "install", str(wheels[0])], check=True)
    subprocess.run(
        [str(python), "-c", "import sag; import sag.main; assert hasattr(sag.main, 'cli')"],
        check=True,
    )
    subprocess.run([str(sag_bin), "--help"], check=True)
