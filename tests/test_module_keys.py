# tests/test_module_keys.py
"""The one grammar for a module name, on both sides of the CI comparison.

CI JUnit pools and Gradle task lines spell a module as a Gradle project path
(`:connect:api`); SAG's evidence trees and receipts spell it as a directory
(`connect/api`); Maven prints a reactor display name (`Apache Camel :: Core`)
on both sides. One key, so a count and its identity cannot disagree by
punctuation alone.
"""

import pytest

from sag.metrics.module_keys import module_key, module_keys


@pytest.mark.parametrize(
    "raw, key",
    [
        (":connect:api", "connect/api"),
        ("connect/api", "connect/api"),
        ("connect/api/", "connect/api"),
        ("./connect/api", "connect/api"),
        (":clients", "clients"),
        (":root", "."),
        (":", "."),
        (".", "."),
        ("./", "."),
        ("ignite-checkstyle", "ignite-checkstyle"),
        ("Apache Camel :: Core", "Apache Camel :: Core"),
        ("  Jackrabbit   JCR Commons ", "Jackrabbit JCR Commons"),
        (":storage:storage-api", "storage/storage-api"),
    ],
)
def test_module_key_canonical_forms(raw, key):
    assert module_key(raw) == key


def test_module_key_is_idempotent():
    for raw in (":connect:api", "connect/api", "Apache Camel :: Core", ":root"):
        assert module_key(module_key(raw)) == module_key(raw)


@pytest.mark.parametrize("raw", ["", "   ", None])
def test_an_empty_identity_is_refused(raw):
    with pytest.raises(ValueError):
        module_key(raw)


def test_module_keys_sorts_and_dedupes_across_spellings():
    assert module_keys([":connect:api", "connect/api", ":clients", "clients/"]) == (
        "clients",
        "connect/api",
    )
