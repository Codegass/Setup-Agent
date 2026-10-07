from scripts.benchmark_eclipse_ci_directory import parse_directory


def test_official_project_instance_join_keeps_namespaces():
    rows = parse_directory(b'<table><tr><th>Project name</th><th>JIPP name</th></tr>'
        b'<tr><td>ee4j.jersey</td><td><a href="/jersey/">jersey</a></td><td>up</td></tr>'
        b'<tr><td>technology.jgit</td><td><a href="https://ci.eclipse.org/jgit/">jgit</a></td></tr></table>')
    assert rows == [{"project_id":"ee4j.jersey","jipp_name":"jersey","url":"https://ci.eclipse.org/jersey/"},
                    {"project_id":"technology.jgit","jipp_name":"jgit","url":"https://ci.eclipse.org/jgit/"}]


def test_other_hosts_and_ambiguous_rows_do_not_create_instances():
    assert parse_directory(b'<table><tr><td>foo</td><td><a href="https://example.org/job/">x</a></td></tr>'
        b'<tr><td>bar</td><td><a href="/one/">a</a><a href="/two/">b</a></td></tr></table>') == []


def test_duplicate_project_entries_are_not_silently_deduplicated():
    rows = parse_directory(b'<table><tr><td>project</td><td><a href="/one/">a</a></td></tr>'
        b'<tr><td>project</td><td><a href="/two/">b</a></td></tr></table>')
    assert len(rows) == 2
