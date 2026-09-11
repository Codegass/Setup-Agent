"""Physical Maven output checks must use archive extensions, not packaging names."""

import subprocess
import zipfile

import pytest

from sag.agent.physical_validator import PhysicalValidator


def _validator(root):
    validator = PhysicalValidator(project_path=str(root))

    def execute(command, _description):
        result = subprocess.run(
            ["/bin/sh", "-c", command], capture_output=True, text=True, cwd=root
        )
        return {"success": result.returncode == 0, "output": result.stdout}

    validator._execute_command_with_logging = execute
    return validator


def _archive(root, name):
    target = root / "target"
    target.mkdir(exist_ok=True)
    path = target / name
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("META-INF/MANIFEST.MF", "Manifest-Version: 1.0\n")
    return path


@pytest.mark.parametrize(
    "packaging,extension",
    [("bundle", "jar"), ("maven-plugin", "jar"), ("ejb", "jar"), ("jar", "jar"), ("war", "war")],
)
def test_real_archive_satisfies_the_expected_output_path(tmp_path, packaging, extension):
    artifact = _archive(tmp_path, f"component-1.0.{extension}")
    pom = (
        "<project><artifactId>component</artifactId><version>1.0</version>"
        f"<packaging>{packaging}</packaging></project>"
    )
    validator = _validator(tmp_path)
    expected = validator._parse_single_maven_expected_artifacts(str(tmp_path), pom)
    assert [item["path"] for item in expected] == [str(artifact)]
    assert validator._verify_expected_artifacts(str(tmp_path), expected)["all_present"]

    artifact.unlink()
    assert not validator._verify_expected_artifacts(str(tmp_path), expected)["all_present"]


@pytest.mark.parametrize("packaging", ["bundle", "maven-plugin", "ejb"])
def test_inherited_version_can_be_read_from_the_real_archive(tmp_path, packaging):
    artifact = _archive(tmp_path, "component-5.9.1-SNAPSHOT.jar")
    pom = (
        "<project><parent><artifactId>parent</artifactId><version>5.9.1-SNAPSHOT</version>"
        "</parent><artifactId>component</artifactId>"
        f"<packaging>{packaging}</packaging></project>"
    )
    validator = _validator(tmp_path)
    expected = validator._parse_single_maven_expected_artifacts(str(tmp_path), pom)
    assert [item["path"] for item in expected] == [str(artifact)]
    assert validator._verify_expected_artifacts(str(tmp_path), expected)["all_present"]


def test_file_named_after_packaging_does_not_substitute_for_bundle_jar(tmp_path):
    _archive(tmp_path, "component-1.0.bundle")
    pom = (
        "<project><artifactId>component</artifactId><version>1.0</version>"
        "<packaging>bundle</packaging></project>"
    )
    validator = _validator(tmp_path)
    expected = validator._parse_single_maven_expected_artifacts(str(tmp_path), pom)
    result = validator._verify_expected_artifacts(str(tmp_path), expected)
    assert result["all_present"] is False
    assert result["missing"] == ["component-1.0.jar"]
