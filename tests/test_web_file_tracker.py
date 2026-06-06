from pathlib import Path

from sag.web.file_tracker import FileChangeTracker


def test_file_tracker_detects_added_modified_and_deleted(tmp_path: Path):
    root = tmp_path / "workspace"
    root.mkdir()
    keep = root / "keep.txt"
    gone = root / "gone.txt"
    keep.write_text("one", encoding="utf-8")
    gone.write_text("old", encoding="utf-8")

    tracker = FileChangeTracker(root)
    base = tracker.snapshot("base")

    keep.write_text("two", encoding="utf-8")
    gone.unlink()
    (root / "new.txt").write_text("new", encoding="utf-8")

    head = tracker.snapshot("head")
    digest = tracker.diff(base, head)

    changes = {item.path: item.change for item in digest.items}
    assert changes["keep.txt"] == "modified"
    assert changes["gone.txt"] == "deleted"
    assert changes["new.txt"] == "added"
    assert digest.counts.modified == 1
    assert digest.counts.deleted == 1
    assert digest.counts.added == 1


def test_file_tracker_ignores_heavy_and_hidden_generated_dirs(tmp_path: Path):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / ".git").mkdir()
    (root / ".git" / "index").write_text("ignored", encoding="utf-8")
    (root / "target").mkdir()
    (root / "target" / "app.jar").write_text("ignored", encoding="utf-8")
    (root / "src.py").write_text("tracked", encoding="utf-8")

    tracker = FileChangeTracker(root)
    snap = tracker.snapshot("base")

    assert "src.py" in snap.files
    assert ".git/index" not in snap.files
    assert "target/app.jar" not in snap.files
