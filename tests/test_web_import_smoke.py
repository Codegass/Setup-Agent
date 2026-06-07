def test_web_package_imports():
    import sag.web
    from sag.web.paths import STATIC_DIR

    assert sag.web.__all__ == ["STATIC_DIR"]
    assert STATIC_DIR.name == "static"
