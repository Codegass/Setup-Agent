import importlib


def test_import_sag_package():
    module = importlib.import_module("sag")
    assert module is not None


def test_import_core_runtime_modules():
    module_names = [
        "sag.main",
        "sag.agent.react_engine",
        "sag.tools.base",
        "sag.reporting",
        "sag.testcases.catalog",
        "sag.ui.diagnosis",
        "sag.ui.events",
        "sag.ui.state",
    ]

    for module_name in module_names:
        assert importlib.import_module(module_name) is not None


def test_cli_object_loads():
    main = importlib.import_module("sag.main")
    assert hasattr(main, "cli")
