"""Archive the official Eclipse project-ID to Jenkins-instance directory."""
from __future__ import annotations

import argparse
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from scripts.benchmark_ci_recovery import Collector, ref, write


DIRECTORY_URL = "https://ci.eclipse.org/"


class DirectoryParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.cells = None
        self.cell = None
        self.rows = []

    def handle_starttag(self, tag, attrs):
        if tag == "tr": self.cells = []
        if tag == "td" and self.cells is not None:
            self.cell = {"text": [], "links": []}
        if tag == "a" and self.cell is not None:
            href = dict(attrs).get("href")
            if href: self.cell["links"].append(urljoin(DIRECTORY_URL, href))

    def handle_data(self, data):
        if self.cell is not None: self.cell["text"].append(data)

    def handle_endtag(self, tag):
        if tag == "td" and self.cell is not None:
            self.cells.append(self.cell); self.cell = None
        if tag == "tr" and self.cells is not None:
            if len(self.cells) >= 2:
                project = " ".join("".join(self.cells[0]["text"]).split())
                name = " ".join("".join(self.cells[1]["text"]).split())
                links = [url for url in self.cells[1]["links"] if urlsplit(url).hostname == "ci.eclipse.org"
                         and urlsplit(url).path not in {"", "/"}]
                if project and name and len(links) == 1:
                    self.rows.append({"project_id": project, "jipp_name": name, "url": links[0]})
            self.cells = None


def parse_directory(raw):
    parser = DirectoryParser(); parser.feed(raw.decode("utf-8"))
    return parser.rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("output/java-benchmark-org-expansion-20260922/ci"))
    parser.add_argument("--collect", action="store_true")
    args = parser.parse_args(); out = args.output.resolve()
    collector = Collector(out, args.collect)
    response = collector.get(DIRECTORY_URL)
    rows = parse_directory((out/response["body"]["path"]).read_bytes()) if response["status"] == "available" else []
    result = {"schema":"eclipse-official-ci-directory-v1", "source":response,
        "generation_script":ref(Path(__file__).resolve(), Path(__file__).resolve().parents[1]),
        "status":"directory_available" if rows else "directory_unavailable_or_unparsed",
        "instances":rows, "scope":"Official project-ID/JIPP association only; no repository, job, checkout or task qualification inferred"}
    write(out/"eclipse-directory.json", result)
    print({"status":result["status"],"listed_instances":len(rows)})


if __name__ == "__main__": main()
