"""Complete verdict-v4 rate blocks for strict live-authority fixtures."""


def complete_verdict_rates(verdict: str) -> dict:
    """Return the smallest complete rate block deriving ``verdict``.

    These are authority-transport fixtures, not rate-calculation fixtures. The
    production rate builders are exercised separately; here we only need a
    closed v4 payload whose legacy word reconciles mechanically with its bands.
    """

    if verdict == "success":
        build_modules = {
            "rate": 100.0,
            "band": "fully",
            "numerator": 1,
            "denominator": 1,
        }
        test_cases = {
            "rate": 100.0,
            "band": "fully",
            "numerator": 1,
            "denominator": 1,
        }
    elif verdict == "failed":
        build_modules = {
            "rate": 0.0,
            "band": "none",
            "numerator": 0,
            "denominator": 1,
        }
        test_cases = {
            "band": "unavailable",
            "reason": "fixture discovery unavailable",
        }
    elif verdict == "partial":
        build_modules = {
            "rate": 100.0,
            "band": "fully",
            "numerator": 1,
            "denominator": 1,
        }
        test_cases = {
            "band": "unavailable",
            "reason": "fixture discovery unavailable",
        }
    else:
        raise ValueError(f"v4 rates cannot derive verdict {verdict!r}")

    return {
        "build": {
            "modules": build_modules,
            "classes": {
                "band": "unavailable",
                "reason": "fixture class census unavailable",
            },
        },
        "test": {
            "cases": test_cases,
            "modules": {
                "band": "unavailable",
                "reason": "fixture test-module survey unavailable",
            },
        },
        "coverage": {
            "status": "unavailable",
            "reason": "fixture coverage not collected",
        },
    }
