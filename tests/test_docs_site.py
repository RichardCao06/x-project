from __future__ import annotations

import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit


ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"


class LinkCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        href = dict(attrs).get("href")
        if href:
            self.links.append(href)


def test_documentation_catalog_and_generated_pages_are_current() -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "build_docs_site.py"), "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (DOCS / "index.html").is_file()
    assert (DOCS / "README.md").is_file()
    for source in DOCS.glob("*.md"):
        if source.name != "README.md":
            assert source.with_suffix(".html").is_file(), source.name


def test_local_documentation_links_resolve() -> None:
    broken: list[str] = []
    for page in DOCS.glob("*.html"):
        collector = LinkCollector()
        collector.feed(page.read_text(encoding="utf-8"))
        for href in collector.links:
            parsed = urlsplit(href)
            if parsed.scheme or parsed.netloc or not parsed.path:
                continue
            target = (page.parent / unquote(parsed.path)).resolve()
            if not target.is_file():
                broken.append(f"{page.name}: {href}")
    assert broken == []


def test_committed_documentation_does_not_link_to_runtime_state() -> None:
    runtime_links: list[str] = []
    for page in DOCS.glob("*.html"):
        collector = LinkCollector()
        collector.feed(page.read_text(encoding="utf-8"))
        for href in collector.links:
            parsed = urlsplit(href)
            if parsed.scheme or parsed.netloc or not parsed.path:
                continue
            target = (page.parent / unquote(parsed.path)).resolve()
            try:
                parts = target.relative_to(ROOT).parts
            except ValueError:
                parts = ()
            if parts and parts[0] == "var":
                runtime_links.append(f"{page.name}: {href}")
    assert runtime_links == []
