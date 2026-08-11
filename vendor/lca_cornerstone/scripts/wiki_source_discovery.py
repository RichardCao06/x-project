#!/usr/bin/env python3
"""Deterministic, bounded Search/Fetch for frozen Wiki claims.

This program is the production network boundary of the node-Wiki provenance
pipeline.  It never verifies a claim.  ``plan`` creates a claim-level,
source-first query queue; ``run`` performs counted/cached searches and fetches
and emits frozen evidence; ``materialize`` embeds that evidence in the
Verify-only Workflow (whose agents have no SearchFetch phase).

The input to ``plan`` is a JSON object with ``claims``.  Each item is either a
frozen claim or a Wiki workflow result row containing the claim under
``item.claim``.  Search strings are derived solely from ``believed_source`` --
``claim_text`` is deliberately never used to construct a query.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import hashlib
import html
import http.client
import ipaddress
import json
import math
import re
import socket
import ssl
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Iterable


ROOT = Path(__file__).resolve().parents[1]
QUEUE_PROTOCOL = "wiki-source-query-v1"
EVIDENCE_PROTOCOL = "wiki-source-evidence-v1"
CACHE_PROTOCOL = "wiki-source-cache-v1"
FROZEN_SEARCH_PROTOCOL = "wiki-frozen-search-v1"
SEARCH_ENDPOINT = "https://html.duckduckgo.com/html/"

# Absolute safety ceilings.  CLI values can make a run smaller, never larger.
ABS_MAX_CLAIMS = 200
ABS_MAX_SEARCHES = 200
ABS_MAX_FETCHES = 400
ABS_MAX_CANDIDATES_PER_CLAIM = 5
ABS_MAX_SEARCH_BYTES = 2_000_000
ABS_MAX_FETCH_BYTES = 12_000_000
ABS_MAX_EXCERPT_CHARS = 40_000
ABS_MAX_REDIRECTS = 5
ABS_MAX_TIMEOUT_SECONDS = 30.0
# Some managed desktop runtimes transparently map approved external hosts into
# RFC 2544's 198.18.0.0/15 benchmark range.  It stays rejected by default and
# can only be enabled explicitly for DNS answers (never for literal URLs).
ALLOW_SYNTHETIC_PROXY_DNS = False


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} 顶层必须是 JSON object")
    return value


def write_json(path: Path, data: dict[str, Any]) -> None:
    """Atomically write a deterministic JSON artifact."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as handle:
        handle.write(payload)
        temporary = Path(handle.name)
    temporary.replace(path)


def file_record(path: Path) -> dict[str, str]:
    resolved = path.resolve()
    return {"path": str(resolved), "sha256": sha256_bytes(resolved.read_bytes())}


def assert_file_record(record: dict[str, Any], label: str) -> Path:
    path = Path(str(record.get("path", ""))).resolve()
    if not path.exists() or sha256_bytes(path.read_bytes()) != record.get("sha256"):
        raise ValueError(f"{label} artifact 缺失或 hash 漂移: {path}")
    return path


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def stable_hash(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


def claims_scope_hash(claims: list[dict[str, Any]]) -> str:
    ordered = sorted(claims, key=lambda item: str(item.get("claim_id", "")))
    payload = json.dumps(ordered, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return stable_hash("frozen-claims-v1", payload)


def bounded(value: int | float, ceiling: int | float, name: str) -> int | float:
    if value <= 0:
        raise ValueError(f"{name} 必须大于 0")
    return min(value, ceiling)


def normalize_source(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _locator_topic(locator: str) -> str:
    value = re.sub(
        r"\b(?:section|chapter|table|page|pages|p|pp|figure|fig)\.?\s*\d+(?:\.\d+)*\b",
        " ",
        locator,
        flags=re.I,
    )
    value = re.sub(r"\b(?:description|overview|discussion|clause)\b", " ", value, flags=re.I)
    # These are locator instructions, not source vocabulary.  Treating
    # ``product title`` as two semantic anchors makes a documentation-index
    # paragraph outrank the actual first-page system heading.
    value = re.sub(r"\b(?:product\s+title|document\s+title)\b", " ", value, flags=re.I)
    return normalize_source(re.sub(r"[^\w\u3400-\u9fff-]+", " ", value))


def source_first_query(
    believed_source: str,
    node_identity: dict[str, Any] | None = None,
    believed_locator: str = "",
) -> str:
    """Identify the source, then localize the target node without claim text.

    Claim prose remains excluded to avoid confirmation-biased searching.  The
    frozen node identity and locator are safe localization terms after source
    identity, and prevent a broad handbook/PDF result from becoming an
    irrelevant first-page excerpt.
    """
    source = normalize_source(believed_source)
    if not source:
        raise ValueError("believed_source 不能为空")
    identity = node_identity if isinstance(node_identity, dict) else {}
    display_name = normalize_source(str(identity.get("display_name", "")))
    locator_topic = _locator_topic(believed_locator)
    parts = [source, display_name, locator_topic]
    return normalize_source(" ".join(part.replace(chr(34), " ") for part in parts if part))


def query_for_claim(claim: dict[str, Any]) -> str:
    return source_first_query(
        str(claim.get("believed_source", "")),
        claim.get("node_identity") if isinstance(claim.get("node_identity"), dict) else None,
        str(claim.get("believed_locator", "")),
    )


def is_internal_modeling_judgment(claim: dict[str, Any]) -> bool:
    source = normalize_source(str(claim.get("believed_source", "")))
    kind = str(claim.get("claim_kind", "")).strip().lower()
    return (
        kind in {"modeling_judgment", "internal_graph_fact", "evidence_gap"}
        or source.upper() == "INTERNAL_MODELING_JUDGMENT"
        or source.upper() == "LCA-CORNERSTONE_GRAPH"
        # Repair claims inherit the registered source title rather than the
        # extract-only claim_kind field.  Repository graph assertions are
        # internal modeling evidence and must never be sent to public search.
        or source.lower().startswith("lca-cornerstone ")
    )


def frozen_claims(document: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
    payload = document.get("result") if isinstance(document.get("result"), dict) else document
    rows = payload.get("claims") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise ValueError("输入必须包含 claims 数组")
    mode = str((payload.get("protocol") or {}).get("mode") or "repair")
    if mode not in {"extract", "repair"}:
        raise ValueError("claims protocol.mode 必须是 extract/repair")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    required = {
        "claim_id", "node_id", "industry", "section", "claim_text",
        "claim_kind", "node_identity", "believed_source",
    }
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"claims[{index}] 必须是 object")
        claim = row.get("claim") if isinstance(row.get("claim"), dict) else row
        missing = sorted(key for key in required if not str(claim.get(key, "")).strip())
        if missing:
            raise ValueError(f"claims[{index}] 缺少冻结字段: {missing}")
        claim_id = str(claim["claim_id"])
        if claim_id in seen:
            raise ValueError(f"claim_id 重复: {claim_id}")
        seen.add(claim_id)
        kind = str(claim.get("claim_kind", "")).strip()
        if kind not in {"external_fact", "internal_graph_fact", "modeling_judgment", "evidence_gap"}:
            raise ValueError(f"claims[{index}] claim_kind 非法: {kind!r}")
        source = normalize_source(str(claim.get("believed_source", "")))
        identity = claim.get("node_identity")
        if not isinstance(identity, dict) or set(identity) != {
            "display_name", "node_type", "facets", "boundary"
        }:
            raise ValueError(f"claims[{index}] node_identity 非法")
        if kind == "internal_graph_fact" and source != "LCA-CORNERSTONE_GRAPH":
            raise ValueError(f"claims[{index}] internal_graph_fact 来源必须是 LCA-CORNERSTONE_GRAPH")
        if kind in {"modeling_judgment", "evidence_gap"} and source != "INTERNAL_MODELING_JUDGMENT":
            raise ValueError(f"claims[{index}] {kind} 来源必须是 INTERNAL_MODELING_JUDGMENT")
        # Round-trip the full frozen claim, including hash locks and old tags.
        result.append(dict(claim))
    return result, mode


def normalized_domains(values: Iterable[str] | None) -> list[str]:
    result: list[str] = []
    for value in values or []:
        domain = value.strip().lower().rstrip(".")
        if "://" in domain:
            parsed = urllib.parse.urlsplit(domain)
            domain = (parsed.hostname or "").lower().rstrip(".")
        if not domain or not re.fullmatch(r"[a-z0-9.-]+", domain):
            raise ValueError(f"非法域名白名单项: {value!r}")
        if domain not in result:
            result.append(domain)
    return sorted(result)


def domain_allowed(host: str, allowlist: Iterable[str] | None) -> bool:
    domains = list(allowlist or [])
    return not domains or any(host == item or host.endswith(f".{item}") for item in domains)


def _public_address(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return False
    if ALLOW_SYNTHETIC_PROXY_DNS and address in ipaddress.ip_network("198.18.0.0/15"):
        return True
    # is_global excludes private, loopback, link-local, multicast, reserved,
    # unspecified and documentation-only address ranges on supported Pythons.
    return address.is_global


Resolver = Callable[..., list[tuple[Any, ...]]]


def validate_external_url(
    url: str,
    allowlist: Iterable[str] | None = None,
    resolver: Resolver | None = None,
) -> str:
    """Reject non-http(s), local/private DNS, credentials and odd ports."""
    resolver = resolver or socket.getaddrinfo
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise ValueError("URL 只允许 http(s)")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("URL 不允许内嵌凭据")
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        raise ValueError("URL 缺少 host")
    if host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
        raise ValueError("URL 指向 localhost/本地域")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("URL port 非法") from exc
    expected_port = 443 if parsed.scheme.lower() == "https" else 80
    if port not in {None, expected_port}:
        raise ValueError("URL 只允许与 scheme 匹配的默认端口")
    if not domain_allowed(host, allowlist):
        raise ValueError(f"URL host 不在域名白名单: {host}")

    try:
        literal = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        literal = None
    if literal is not None:
        if not literal.is_global:
            raise ValueError("URL 指向私网/环回/保留地址")
    else:
        try:
            records = resolver(host, port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
        except OSError as exc:
            raise ValueError(f"URL DNS 解析失败: {host}") from exc
        addresses = {str(record[4][0]) for record in records if len(record) > 4 and record[4]}
        if not addresses:
            raise ValueError(f"URL DNS 无地址: {host}")
        if any(not _public_address(address) for address in addresses):
            raise ValueError(f"URL DNS 命中私网/环回/保留地址: {host}")
    # Strip fragments: they are not part of the fetched representation and
    # would otherwise defeat URL de-duplication.
    canonical_host = f"[{host}]" if ":" in host else host
    return urllib.parse.urlunsplit((parsed.scheme.lower(), canonical_host, parsed.path or "/", parsed.query, ""))


def resolve_external_url(
    url: str,
    allowlist: Iterable[str] | None = None,
    resolver: Resolver | None = None,
) -> tuple[str, list[str]]:
    """Validate and return the exact public addresses approved for connection."""
    resolver = resolver or socket.getaddrinfo
    captured: list[tuple[Any, ...]] = []

    def capture(host: str, port: int, **kwargs: Any) -> list[tuple[Any, ...]]:
        records = resolver(host, port, **kwargs)
        captured.extend(records)
        return records

    safe_url = validate_external_url(url, allowlist, capture)
    parsed = urllib.parse.urlsplit(safe_url)
    try:
        literal = ipaddress.ip_address((parsed.hostname or "").split("%", 1)[0])
    except ValueError:
        literal = None
    addresses = (
        [str(literal)] if literal is not None
        else sorted({str(record[4][0]) for record in captured if len(record) > 4 and record[4]})
    )
    if not addresses or any(not _public_address(address) for address in addresses):
        raise ValueError("URL 未解析到纯公网地址集")
    return safe_url, addresses


class PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host: str, port: int, address: str, timeout: float):
        super().__init__(host, port=port, timeout=timeout)
        self._pinned_address = address

    def connect(self) -> None:
        self.sock = socket.create_connection(
            (self._pinned_address, self.port), self.timeout, self.source_address
        )


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, port: int, address: str, timeout: float):
        super().__init__(host, port=port, timeout=timeout, context=ssl.create_default_context())
        self._pinned_address = address

    def connect(self) -> None:
        raw = socket.create_connection(
            (self._pinned_address, self.port), self.timeout, self.source_address
        )
        # TLS authenticates the original hostname while the TCP socket stays
        # pinned to the already-approved public address (no second DNS lookup).
        self.sock = self._context.wrap_socket(raw, server_hostname=self.host)


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Validate every redirect target before urllib follows it."""

    def __init__(self, allowlist: Iterable[str] | None, resolver: Resolver | None, max_redirects: int):
        super().__init__()
        self.allowlist = list(allowlist or [])
        self.resolver = resolver or socket.getaddrinfo
        self.max_redirects = max_redirects
        self.redirects = 0

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Any:
        self.redirects += 1
        if self.redirects > self.max_redirects:
            raise urllib.error.HTTPError(newurl, code, "redirect hard limit exceeded", headers, fp)
        safe_url = validate_external_url(newurl, self.allowlist, self.resolver)
        return super().redirect_request(req, fp, code, msg, headers, safe_url)


def safe_http_get(
    url: str,
    *,
    timeout: float,
    max_bytes: int,
    allowlist: Iterable[str] | None = None,
    resolver: Resolver | None = None,
    max_redirects: int = ABS_MAX_REDIRECTS,
) -> tuple[bytes, str, str, int]:
    """Fetch an external URL with pre/post/redirect validation and byte cap."""
    resolver = resolver or socket.getaddrinfo
    current = url
    for redirect_count in range(max_redirects + 1):
        safe_url, addresses = resolve_external_url(current, allowlist, resolver)
        parsed = urllib.parse.urlsplit(safe_url)
        host = parsed.hostname or ""
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        target = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
        response = None
        last_error: Exception | None = None
        request_headers = {
            # Keep the client identifiable while using a browser-compatible
            # prefix accepted by common public-document CDNs.
            "User-Agent": (
                "Mozilla/5.0 (compatible; "
                "lca-cornerstone-wiki-source-discovery/1.0)"
            ),
            "Accept-Encoding": "identity",
            "Accept-Language": "en",
        }
        # Publications Office resource URLs use HTTP content negotiation.
        # Without this, an `.xhtml` manifestation resolves to RDF metadata.
        if parsed.path.lower().endswith(".xhtml"):
            request_headers["Accept"] = "application/xhtml+xml"
        elif parsed.path.lower().endswith((".pdf", ".pdfa1a")):
            request_headers["Accept"] = "application/pdf"
        else:
            request_headers["Accept"] = "text/html,application/xhtml+xml;q=0.9,*/*;q=0.1"
        for address in addresses:
            connection: http.client.HTTPConnection
            connection = (
                PinnedHTTPSConnection(host, port, address, timeout)
                if parsed.scheme == "https"
                else PinnedHTTPConnection(host, port, address, timeout)
            )
            try:
                connection.request(
                    "GET", target,
                    headers=request_headers,
                )
                response = connection.getresponse()
                break
            except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
                last_error = exc
                connection.close()
        if response is None:
            raise ValueError(f"all pinned public addresses failed: {last_error}")
        try:
            if response.status in {301, 302, 303, 307, 308}:
                location = response.getheader("Location")
                if not location:
                    raise ValueError("redirect missing Location")
                if redirect_count >= max_redirects:
                    raise ValueError("redirect hard limit exceeded")
                current = urllib.parse.urljoin(safe_url, location)
                continue
            if response.status >= 400:
                raise ValueError(f"HTTP status {response.status}")
            payload = response.read(max_bytes + 1)
            if len(payload) > max_bytes:
                raise ValueError("response exceeds max_bytes")
            content_type = str(response.getheader("Content-Type", "application/octet-stream")).split(";", 1)[0]
            if content_type in {"text/html", "application/xhtml+xml"}:
                match = re.search(
                    rb"location\.replace\(\s*[\"']([^\"']+)[\"']\s*\)", payload, re.I
                )
                if match:
                    if redirect_count >= max_redirects:
                        raise ValueError("redirect hard limit exceeded")
                    current = urllib.parse.urljoin(
                        safe_url,
                        html.unescape(match.group(1).decode("utf-8", errors="replace")),
                    )
                    redirect_count += 1
                    continue
            return payload, safe_url, content_type, redirect_count
        finally:
            response.close()
            connection.close()
    raise ValueError("redirect hard limit exceeded")


class ResultParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.results: list[dict[str, str]] = []
        self.current: dict[str, str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "a" and "result__a" in (values.get("class") or "").split():
            href = values.get("href") or ""
            parsed = urllib.parse.urlsplit(href)
            target = urllib.parse.parse_qs(parsed.query).get("uddg", [href])[0]
            self.current = {"url": urllib.parse.unquote(target), "title": ""}

    def handle_data(self, data: str) -> None:
        if self.current is not None:
            self.current["title"] += data

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self.current is not None:
            self.current["title"] = html.unescape(self.current["title"]).strip()
            self.results.append(self.current)
            self.current = None


class TextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript", "svg"}:
            self.hidden += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript", "svg"} and self.hidden:
            self.hidden -= 1

    def handle_data(self, data: str) -> None:
        if not self.hidden and data.strip():
            self.parts.append(data.strip())


def parse_search_results(payload: bytes) -> list[dict[str, str]]:
    parser = ResultParser()
    parser.feed(payload.decode("utf-8", errors="replace"))
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in parser.results:
        url = item.get("url", "")
        if url in seen:
            continue
        seen.add(url)
        result.append({"url": url, "title": item.get("title", "")})
    return result


def _locator_anchors(locator: str) -> list[str]:
    anchors: list[str] = []
    for pattern in (
        r"Table\s+\d+(?:\.\d+)*",
        r"§+\s*\d+(?:\.\d+)*",
        r"Section\s+\d+(?:\.\d+)*",
        r"Chapter\s+\d+(?:\.\d+)*",
        r"Article\s+\d+(?:\.\d+)*",
        r"Annex\s+[IVXLC]+",
        r"point\s+\d+(?:\.\d+)*(?:\([a-z]\))?",
        r"(?:Pages?|pp?\.)\s*\d+(?:\s*[-–]\s*\d+)?",
    ):
        for match in re.findall(pattern, locator, re.I):
            anchor = match.lstrip("§").strip()
            if anchor not in anchors:
                anchors.append(anchor)
            number = re.search(r"\d+(?:\.\d+)*", anchor)
            if number and number.group(0) not in anchors:
                anchors.append(number.group(0))
    topic = _locator_topic(locator)
    if topic:
        # Function words otherwise create thousands of false high-scoring
        # windows in long PDFs (for example the locator phrase ``product title
        # and system overview`` used to rank every occurrence of ``and``).
        stopwords = {"and", "or", "the", "of", "for", "to", "in", "on", "with", "by", "from"}
        tokens = [token for token in topic.split() if len(token) >= 3 and token.lower() not in stopwords]
        for size in (min(4, len(tokens)), 3, 2, 1):
            if size <= 0 or len(tokens) < size:
                continue
            for start in range(len(tokens) - size + 1):
                anchor = " ".join(tokens[start:start + size])
                if anchor not in anchors:
                    anchors.append(anchor)
    return anchors


def extract_excerpt(
    url: str, payload: bytes, content_type: str, max_chars: int, locator: str = ""
) -> tuple[str, str]:
    is_pdf = payload.startswith(b"%PDF") or content_type == "application/pdf" or urllib.parse.urlsplit(url).path.lower().endswith(".pdf")
    if is_pdf:
        with tempfile.NamedTemporaryFile(suffix=".pdf") as handle:
            handle.write(payload)
            handle.flush()
            converted = __import__("subprocess").run(
                ["pdftotext", "-layout", handle.name, "-"],
                check=False,
                capture_output=True,
            )
        if converted.returncode:
            raise ValueError("pdftotext failed")
        text = converted.stdout.decode("utf-8", errors="replace")
        media_type = "application/pdf"
    else:
        parser = TextParser()
        parser.feed(payload.decode("utf-8", errors="replace"))
        text = " ".join(parser.parts)
        media_type = (
            content_type
            if content_type.startswith("text/") or content_type == "application/xhtml+xml"
            else "text/html"
        )
    excerpt = re.sub(r"\s+", " ", text).strip()
    ranked_windows: list[tuple[int, int, str]] = []
    anchors = _locator_anchors(locator)
    for anchor in anchors:
        for match in re.finditer(re.escape(anchor), excerpt, re.I):
            start = max(0, match.start() - 1000)
            end = min(len(excerpt), match.end() + 6000)
            window = excerpt[start:end]
            # Repeated vocabulary is common in long regulations.  Rank every
            # occurrence by the number of independent locator anchors found in
            # its neighborhood; selecting the last occurrence silently drifts
            # from Annex definitions to later tables and recitals.
            score = sum(bool(re.search(re.escape(item), window, re.I)) for item in anchors)
            ranked_windows.append((score, start, window))
    if ranked_windows:
        windows: list[str] = []
        for _, _, window in sorted(ranked_windows, key=lambda row: (-row[0], row[1])):
            if any(window in selected or selected in window for selected in windows):
                continue
            windows.append(window)
            if len(" […] ".join(windows)) >= max_chars:
                break
        excerpt = " […] ".join(windows)
    elif anchors:
        # A locator-bearing claim must not silently receive the document's
        # opening pages.  An empty excerpt is an explicit localization miss
        # and forces source discovery/retry before Verify.
        excerpt = ""
    return media_type, excerpt[:max_chars]


def command_plan(args: argparse.Namespace) -> int:
    input_path = args.claims.resolve()
    claims, mode = frozen_claims(read_json(input_path))
    max_claims = int(bounded(args.max_claims, ABS_MAX_CLAIMS, "max_claims"))
    max_searches = int(bounded(args.max_searches, ABS_MAX_SEARCHES, "max_searches"))
    max_fetches = int(bounded(args.max_fetches, ABS_MAX_FETCHES, "max_fetches"))
    max_candidates = int(bounded(args.max_candidates_per_claim, ABS_MAX_CANDIDATES_PER_CLAIM, "max_candidates_per_claim"))
    allowlist = normalized_domains(args.allow_domain)

    # Internal graph facts, modeling judgments and explicit evidence gaps are
    # not external research failures and consume no query/fetch budget.
    # Preserve them separately so run/materialize can
    # deterministically downgrade them without sending them to Search.
    internal = [item for item in claims if is_internal_modeling_judgment(item)]
    external = [item for item in claims if not is_internal_modeling_judgment(item)]
    selected = external[:max_claims]
    skipped = [
        {"claim_id": item["claim_id"], "reason": "internal_modeling_judgment"}
        for item in internal
    ] + [
        {"claim_id": item["claim_id"], "reason": "batch_claim_budget"}
        for item in external[max_claims:]
    ]
    queue: list[dict[str, Any]] = []
    deferred: list[dict[str, Any]] = [
        {"claim": item, "disposition": "batch_claim_budget"}
        for item in external[max_claims:]
    ]
    source_slots: set[str] = set()
    for claim in selected:
        query = query_for_claim(claim)
        search_key = stable_hash("source-search-v3", query)
        if search_key not in source_slots and len(source_slots) >= max_searches:
            skipped.append({"claim_id": claim["claim_id"], "reason": "batch_search_budget"})
            deferred.append({"claim": claim, "disposition": "batch_search_budget"})
            continue
        source_slots.add(search_key)
        queue.append({
            "query_id": stable_hash(str(claim["claim_id"]), search_key)[:24],
            "claim": claim,
            "query": query,
            "search_hash": search_key,
        })

    output = (args.output or input_path.parent / "source-query-queue.json").resolve()
    artifact = {
        "protocol": {"version": QUEUE_PROTOCOL, "kind": "claim-source-query-queue", "mode": mode},
        "input": str(input_path),
        "input_record": file_record(input_path),
        "claims_total": len(claims),
        "claim_order": [str(item["claim_id"]) for item in claims],
        "claims_scope_sha256": claims_scope_hash(claims),
        "hard_limits": {
            "max_claims": max_claims,
            "max_searches": max_searches,
            "max_fetches": max_fetches,
            "max_candidates_per_claim": max_candidates,
            "allowed_domains": allowlist,
        },
        "queries": queue,
        "non_external_claims": [
            {"claim": item, "disposition": "internal_modeling_judgment"}
            for item in internal
        ],
        "deferred_claims": deferred,
        "skipped": skipped,
        "budget_exceeded": any(item["reason"].startswith("batch_") for item in skipped),
    }
    write_json(output, artifact)
    print(json.dumps({"output": str(output), "claims": len(queue), "unique_queries": len(source_slots), "skipped": len(skipped)}, ensure_ascii=False))
    return 0


def _cache_read(path: Path, kind: str, identity: str) -> dict[str, Any] | None:
    if not path.exists():
        return None
    cached = read_json(path)
    protocol = cached.get("protocol") or {}
    if protocol.get("version") != CACHE_PROTOCOL or protocol.get("kind") != kind or cached.get("identity") != identity:
        return None
    return cached


def _cached_fetch_is_safe(
    cached: dict[str, Any],
    payload_path: Path,
    allowlist: list[str],
    max_fetch_bytes: int,
    max_excerpt_chars: int,
) -> bool:
    record = cached.get("record")
    if not isinstance(record, dict):
        return False
    if record.get("status") != "fetched":
        return record.get("status") in {"error", "empty"}
    try:
        validate_external_url(str(record.get("url", "")), allowlist)
        payload = payload_path.read_bytes()
        if len(payload) > max_fetch_bytes or sha256_bytes(payload) != record.get("content_sha256"):
            return False
        _, excerpt = extract_excerpt(
            str(record["url"]), payload, str(record.get("content_type", "")), max_excerpt_chars,
            str(record.get("excerpt_locator", "")),
        )
    except (OSError, ValueError):
        return False
    return bool(excerpt) and excerpt == record.get("excerpt")


def write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("wb", dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
        handle.write(payload)
        temporary = Path(handle.name)
    temporary.replace(path)


def _search_once(
    query: str,
    *,
    timeout: float,
    max_bytes: int,
    http_get: Callable[..., tuple[bytes, str, str, int]],
) -> list[dict[str, str]]:
    url = SEARCH_ENDPOINT + "?" + urllib.parse.urlencode({"q": query})
    payload, _, _, _ = http_get(
        url,
        timeout=timeout,
        max_bytes=max_bytes,
        allowlist=["duckduckgo.com"],
        max_redirects=ABS_MAX_REDIRECTS,
    )
    return parse_search_results(payload)


def frozen_search_records(
    document: dict[str, Any], queue: dict[str, Any]
) -> tuple[dict[str, dict[str, Any]], str, float]:
    """Validate a counted platform-Search handoff against the frozen queue."""
    protocol = document.get("protocol") or {}
    if (
        protocol.get("version") != FROZEN_SEARCH_PROTOCOL
        or protocol.get("kind") != "query-search-results"
    ):
        raise ValueError("frozen search protocol 非法")
    backend = str(document.get("backend", "")).strip()
    if not backend:
        raise ValueError("frozen search 缺 backend")
    rows = document.get("queries")
    if not isinstance(rows, list):
        raise ValueError("frozen search queries 必须是数组")
    expected = {
        str(item["search_hash"]): str(item["query"])
        for item in queue.get("queries", [])
    }
    records: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"frozen search queries[{index}] 非法")
        identity = str(row.get("search_hash", ""))
        query = str(row.get("query", ""))
        if identity in records or expected.get(identity) != query:
            raise ValueError(f"frozen search queries[{index}] identity/query 漂移")
        results = row.get("results")
        if not isinstance(results, list) or any(
            not isinstance(item, dict)
            or not str(item.get("url", "")).strip()
            or not isinstance(item.get("title", ""), str)
            for item in results
        ):
            raise ValueError(f"frozen search queries[{index}] results 非法")
        status = row.get("status")
        if status not in {"found", "not_found"}:
            raise ValueError(f"frozen search queries[{index}] status 非法")
        if (status == "found") != bool(results):
            raise ValueError(f"frozen search queries[{index}] status/results 不一致")
        records[identity] = {"status": status, "results": results}
    if set(records) != set(expected):
        raise ValueError("frozen search 必须精确覆盖 queue 的全部唯一查询")
    usage = document.get("usage")
    if not isinstance(usage, dict):
        raise ValueError("frozen search 缺 usage")
    requests = usage.get("search_requests")
    cost = usage.get("cost_usd")
    if requests != len(records):
        raise ValueError("frozen search search_requests 与唯一查询数不一致")
    if (
        not isinstance(cost, (int, float))
        or isinstance(cost, bool)
        or not math.isfinite(float(cost))
        or cost < 0
    ):
        raise ValueError("frozen search cost_usd 非法")
    return records, backend, float(cost)


def execute_queue(
    queue: dict[str, Any],
    *,
    cache_dir: Path,
    max_searches: int,
    max_fetches: int,
    max_candidates_per_claim: int,
    timeout: float,
    max_search_bytes: int,
    max_fetch_bytes: int,
    max_excerpt_chars: int,
    max_redirects: int,
    allowlist: list[str],
    frozen_searches: dict[str, dict[str, Any]] | None = None,
    frozen_search_backend: str | None = None,
    frozen_search_cost_usd: float = 0.0,
    http_get: Callable[..., tuple[bytes, str, str, int]] = safe_http_get,
) -> dict[str, Any]:
    if (queue.get("protocol") or {}).get("version") != QUEUE_PROTOCOL:
        raise ValueError("queue protocol mismatch")
    items = queue.get("queries")
    if not isinstance(items, list):
        raise ValueError("queue.queries 必须是数组")
    frozen = queue.get("hard_limits") or {}
    if len(items) > int(frozen.get("max_claims", len(items))):
        raise ValueError("queue claims 超过冻结硬限制")
    seen_claim_ids: set[str] = set()
    scoped_claims: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict) or not isinstance(item.get("claim"), dict):
            raise ValueError(f"queue.queries[{index}] 非法")
        claim = item["claim"]
        claim_id = str(claim.get("claim_id", ""))
        if not claim_id or claim_id in seen_claim_ids:
            raise ValueError(f"queue claim_id 缺失或重复: {claim_id!r}")
        seen_claim_ids.add(claim_id)
        scoped_claims.append(claim)
        expected_query = query_for_claim(claim)
        expected_hash = stable_hash("source-search-v3", expected_query)
        expected_id = stable_hash(claim_id, expected_hash)[:24]
        if (
            item.get("query") != expected_query
            or item.get("search_hash") != expected_hash
            or item.get("query_id") != expected_id
        ):
            raise ValueError(f"queue.queries[{index}] source-first identity 漂移")
    for collection, dispositions in (
        (queue.get("non_external_claims", []), {"internal_modeling_judgment"}),
        (queue.get("deferred_claims", []), {"batch_claim_budget", "batch_search_budget"}),
    ):
        if not isinstance(collection, list):
            raise ValueError("queue claim disposition collection 必须是数组")
        for index, entry in enumerate(collection):
            if not isinstance(entry, dict) or not isinstance(entry.get("claim"), dict):
                raise ValueError(f"queue disposition claim[{index}] 非法")
            claim = entry["claim"]
            claim_id = str(claim.get("claim_id", ""))
            if not claim_id or claim_id in seen_claim_ids:
                raise ValueError(f"queue claim_id 缺失或重复: {claim_id!r}")
            if entry.get("disposition") not in dispositions:
                raise ValueError(f"queue disposition claim[{index}] reason 非法")
            internal = is_internal_modeling_judgment(claim)
            if internal != (entry.get("disposition") == "internal_modeling_judgment"):
                raise ValueError(f"queue disposition claim[{index}] 类型路由漂移")
            seen_claim_ids.add(claim_id)
            scoped_claims.append(claim)
    if len(scoped_claims) != queue.get("claims_total") or claims_scope_hash(scoped_claims) != queue.get("claims_scope_sha256"):
        raise ValueError("queue frozen claims scope 漂移")
    # A runner may tighten a frozen queue but may never broaden it.
    max_searches = min(max_searches, int(frozen.get("max_searches", max_searches)))
    max_fetches = min(max_fetches, int(frozen.get("max_fetches", max_fetches)))
    max_candidates_per_claim = min(max_candidates_per_claim, int(frozen.get("max_candidates_per_claim", max_candidates_per_claim)))

    cache_dir.mkdir(parents=True, exist_ok=True)
    search_cache = cache_dir / "search"
    fetch_cache = cache_dir / "fetch"
    search_cache.mkdir(exist_ok=True)
    fetch_cache.mkdir(exist_ok=True)
    usage = {
        "network_queries": 0,
        "network_fetches": 0,
        "cache_hits": 0,
        "search_cache_hits": 0,
        "fetch_cache_hits": 0,
        # The built-in deterministic backend uses public DDG HTML + direct
        # fetch and has no metered API charge.  Freeze that fact explicitly;
        # missing cost is never interpreted as zero downstream.
        "cost_usd": 0.0,
        "search_backend": frozen_search_backend or "duckduckgo-html",
    }
    budget_exceeded = bool(queue.get("budget_exceeded"))

    searches: dict[str, dict[str, Any]] = {}
    for item in items:
        identity = str(item.get("search_hash") or stable_hash("source-search-v3", str(item["query"])))
        if identity in searches:
            continue
        if frozen_searches is not None:
            if usage["network_queries"] >= max_searches:
                budget_exceeded = True
                searches[identity] = {"status": "budget_skipped", "results": []}
                continue
            if identity not in frozen_searches:
                raise ValueError(f"frozen search 缺查询: {identity}")
            searches[identity] = frozen_searches[identity]
            usage["network_queries"] += 1
            continue
        cached = _cache_read(search_cache / f"{identity}.json", "search", identity)
        if cached is not None:
            usage["cache_hits"] += 1
            usage["search_cache_hits"] += 1
            searches[identity] = cached["record"]
            continue
        if usage["network_queries"] >= max_searches:
            budget_exceeded = True
            searches[identity] = {"status": "budget_skipped", "results": []}
            continue
        started = time.monotonic()
        try:
            found = _search_once(str(item["query"]), timeout=timeout, max_bytes=max_search_bytes, http_get=http_get)
            record = {"status": "found" if found else "not_found", "results": found, "elapsed_ms": round((time.monotonic() - started) * 1000)}
        except Exception as exc:  # a network failure is frozen as data
            record = {"status": "error", "error": f"{type(exc).__name__}: {exc}", "results": [], "elapsed_ms": round((time.monotonic() - started) * 1000)}
        usage["network_queries"] += 1
        write_json(search_cache / f"{identity}.json", {"protocol": {"version": CACHE_PROTOCOL, "kind": "search"}, "identity": identity, "record": record})
        searches[identity] = record

    if frozen_searches is not None:
        usage["cost_usd"] = frozen_search_cost_usd

    # Validate/filter candidates before scheduling fetches.  Rejected URLs are
    # counted but never requested.  DNS validation happens again at fetch time.
    rejected_urls = 0
    candidate_urls: dict[str, list[dict[str, str]]] = {}
    for item in items:
        identity = str(item["search_hash"])
        candidates: list[dict[str, str]] = []
        seen: set[str] = set()
        raw_results = searches.get(identity, {}).get("results", [])
        if not isinstance(raw_results, list):
            raw_results = []
        for result in raw_results:
            if not isinstance(result, dict):
                rejected_urls += 1
                continue
            try:
                safe_url = validate_external_url(str(result.get("url", "")), allowlist)
            except ValueError:
                rejected_urls += 1
                continue
            if safe_url in seen:
                continue
            seen.add(safe_url)
            candidates.append({"url": safe_url, "title": str(result.get("title", ""))})
            if len(candidates) == max_candidates_per_claim:
                break
        candidate_urls[str(item["query_id"])] = candidates

    locator_by_url: dict[str, list[str]] = defaultdict(list)
    for item in items:
        locator = str((item.get("claim") or {}).get("believed_locator", ""))
        for candidate in candidate_urls[str(item["query_id"])]:
            if locator and locator not in locator_by_url[candidate["url"]]:
                locator_by_url[candidate["url"]].append(locator)

    fetch_records: dict[str, dict[str, Any]] = {}
    for item in items:
        for candidate in candidate_urls[str(item["query_id"])]:
            url = candidate["url"]
            identity = stable_hash("fetch-v2", url)
            if identity in fetch_records:
                continue
            cached = _cache_read(fetch_cache / f"{identity}.json", "fetch", identity)
            payload_path = fetch_cache / f"{identity}.payload"
            if cached is not None and _cached_fetch_is_safe(
                cached, payload_path, allowlist, max_fetch_bytes, max_excerpt_chars
            ):
                usage["cache_hits"] += 1
                usage["fetch_cache_hits"] += 1
                fetch_records[identity] = cached["record"]
                continue
            if usage["network_fetches"] >= max_fetches:
                budget_exceeded = True
                fetch_records[identity] = {"status": "budget_skipped", "url": url}
                continue
            started = time.monotonic()
            payload: bytes | None = None
            try:
                payload, final_url, content_type, redirects = http_get(
                    url,
                    timeout=timeout,
                    max_bytes=max_fetch_bytes,
                    allowlist=allowlist,
                    max_redirects=max_redirects,
                )
                excerpt_locator = " | ".join(locator_by_url.get(url, []))
                media_type, excerpt = extract_excerpt(
                    final_url, payload, content_type, max_excerpt_chars, excerpt_locator
                )
                record = {
                    "status": "fetched" if excerpt else "empty",
                    "url": final_url,
                    "content_type": media_type,
                    "content_sha256": sha256_bytes(payload),
                    "bytes": len(payload),
                    "excerpt": excerpt,
                    "excerpt_locator": excerpt_locator,
                    "redirects": redirects,
                    "elapsed_ms": round((time.monotonic() - started) * 1000),
                }
            except Exception as exc:
                record = {"status": "error", "url": url, "error": f"{type(exc).__name__}: {exc}", "elapsed_ms": round((time.monotonic() - started) * 1000)}
            usage["network_fetches"] += 1
            if payload is not None and record.get("status") in {"fetched", "empty"}:
                write_bytes(payload_path, payload)
            write_json(fetch_cache / f"{identity}.json", {"protocol": {"version": CACHE_PROTOCOL, "kind": "fetch"}, "identity": identity, "record": record})
            fetch_records[identity] = record

    evidence_claims: list[dict[str, Any]] = []
    for item in items:
        candidates = []
        claim_locator = str((item.get("claim") or {}).get("believed_locator", ""))
        locator_miss = False
        for rank, candidate in enumerate(candidate_urls[str(item["query_id"])], 1):
            identity = stable_hash("fetch-v2", candidate["url"])
            fetched = fetch_records[identity]
            if fetched.get("status") != "fetched":
                locator_miss = locator_miss or (
                    bool(claim_locator) and fetched.get("status") == "empty"
                )
                continue
            # The payload is URL-level, but an excerpt is claim-level.  One
            # URL often supports several claims with different locators; using
            # the URL-level excerpt would silently bind them all to one window.
            payload_path = fetch_cache / f"{identity}.payload"
            payload = payload_path.read_bytes()
            media_type, excerpt = extract_excerpt(
                str(fetched.get("url", candidate["url"])),
                payload,
                str(fetched.get("content_type", "")),
                max_excerpt_chars,
                claim_locator,
            )
            if not excerpt:
                locator_miss = locator_miss or bool(claim_locator)
                continue
            candidates.append({
                "evidence_id": f"ev-{identity[:20]}",
                "payload_path": str(payload_path.resolve()),
                "rank": rank,
                "title": candidate["title"],
                **fetched,
                "content_type": media_type,
                "excerpt": excerpt,
                "excerpt_locator": claim_locator,
            })
        search_record = searches[str(item["search_hash"])]
        evidence_item = {
            "claim": item["claim"],
            "query": {"query_id": item["query_id"], "text": item["query"], "source_first": True, "search_hash": item["search_hash"]},
            "search_status": search_record.get("status", "error"),
            "candidates": candidates,
        }
        if locator_miss and not candidates:
            evidence_item["disposition"] = "locator_miss"
        evidence_claims.append(evidence_item)

    for index, item in enumerate(queue.get("non_external_claims", [])):
        if not isinstance(item, dict) or not isinstance(item.get("claim"), dict):
            raise ValueError(f"queue.non_external_claims[{index}] 非法")
        if item.get("disposition") != "internal_modeling_judgment":
            raise ValueError(f"queue.non_external_claims[{index}] disposition 非法")
        evidence_claims.append({
            "claim": item["claim"],
            "query": None,
            "search_status": "not_applicable",
            "candidates": [],
            "disposition": "internal_modeling_judgment",
        })

    for index, item in enumerate(queue.get("deferred_claims", [])):
        if not isinstance(item, dict) or not isinstance(item.get("claim"), dict):
            raise ValueError(f"queue.deferred_claims[{index}] 非法")
        disposition = item.get("disposition")
        if disposition not in {"batch_claim_budget", "batch_search_budget"}:
            raise ValueError(f"queue.deferred_claims[{index}] disposition 非法")
        evidence_claims.append({
            "claim": item["claim"],
            "query": None,
            "search_status": "budget_skipped",
            "candidates": [],
            "disposition": disposition,
        })

    claim_order = queue.get("claim_order")
    if claim_order is not None:
        if (
            not isinstance(claim_order, list)
            or len(claim_order) != len(evidence_claims)
            or len(set(claim_order)) != len(claim_order)
        ):
            raise ValueError("queue.claim_order 非法")
        by_claim_id = {
            str(item["claim"]["claim_id"]): item for item in evidence_claims
        }
        if set(by_claim_id) != set(claim_order):
            raise ValueError("queue.claim_order 与 evidence claims 不一致")
        evidence_claims = [by_claim_id[claim_id] for claim_id in claim_order]

    all_candidates = [candidate for item in evidence_claims for candidate in item["candidates"]]
    valid_hash = re.compile(r"^[0-9a-f]{64}$")
    compliance = {
        "claims_total": len(evidence_claims),
        "claims_with_evidence": sum(bool(item["candidates"]) for item in evidence_claims),
        "candidates_returned": len(all_candidates),
        "rejected_urls": rejected_urls,
        "http_urls_valid": sum(urllib.parse.urlsplit(item["url"]).scheme in {"http", "https"} for item in all_candidates),
        "quotes_nonempty": sum(bool(str(item.get("excerpt", "")).strip()) for item in all_candidates),
        "content_hashes_valid": sum(bool(valid_hash.fullmatch(str(item.get("content_sha256", "")))) for item in all_candidates),
    }
    compliance["claims_without_evidence"] = compliance["claims_total"] - compliance["claims_with_evidence"]
    # Stable, explicit aliases for Go/No-Go consumers.  Counts (rather than
    # booleans only) make partial artifacts auditable.
    compliance["urls_compliant"] = compliance["http_urls_valid"]
    compliance["quotes_compliant"] = compliance["quotes_nonempty"]
    compliance["content_hashes_compliant"] = compliance["content_hashes_valid"]
    compliance["all_evidence_compliant"] = all(
        compliance[key] == len(all_candidates)
        for key in ("http_urls_valid", "quotes_nonempty", "content_hashes_valid")
    )
    return {
        "protocol": {"version": EVIDENCE_PROTOCOL, "kind": "claim-evidence", "mode": (queue.get("protocol") or {}).get("mode", "repair")},
        "claims": evidence_claims,
        "usage": usage,
        "hard_limits": {
            "max_claims": int(frozen.get("max_claims", len(items))),
            "max_searches": max_searches,
            "max_fetches": max_fetches,
            "max_candidates_per_claim": max_candidates_per_claim,
            "timeout_seconds": timeout,
            "max_search_bytes": max_search_bytes,
            "max_fetch_bytes": max_fetch_bytes,
            "max_excerpt_chars": max_excerpt_chars,
            "max_redirects": max_redirects,
            "allowed_domains": allowlist,
        },
        "budget_exceeded": budget_exceeded,
        "compliance": compliance,
    }


def command_run(args: argparse.Namespace) -> int:
    global ALLOW_SYNTHETIC_PROXY_DNS
    ALLOW_SYNTHETIC_PROXY_DNS = bool(args.allow_synthetic_proxy_dns)
    queue_path = args.queue.resolve()
    queue = read_json(queue_path)
    input_path = assert_file_record(queue.get("input_record") or {}, "queue input claims")
    input_claims, input_mode = frozen_claims(read_json(input_path))
    if (
        input_mode != (queue.get("protocol") or {}).get("mode")
        or claims_scope_hash(input_claims) != queue.get("claims_scope_sha256")
    ):
        raise ValueError("queue 与冻结 input claims 漂移")
    frozen_domains = normalized_domains((queue.get("hard_limits") or {}).get("allowed_domains", []))
    requested_domains = normalized_domains(args.allow_domain)
    if requested_domains and requested_domains != frozen_domains:
        raise ValueError("run 的域名白名单必须与冻结 queue 完全一致")
    allowlist = frozen_domains
    imported_searches = None
    imported_backend = None
    imported_cost = 0.0
    search_results_path = args.search_results.resolve() if args.search_results else None
    if search_results_path is not None:
        imported_searches, imported_backend, imported_cost = frozen_search_records(
            read_json(search_results_path), queue
        )
    evidence = execute_queue(
        queue,
        cache_dir=args.cache_dir.resolve(),
        max_searches=int(bounded(args.max_searches, ABS_MAX_SEARCHES, "max_searches")),
        max_fetches=int(bounded(args.max_fetches, ABS_MAX_FETCHES, "max_fetches")),
        max_candidates_per_claim=int(bounded(args.max_candidates_per_claim, ABS_MAX_CANDIDATES_PER_CLAIM, "max_candidates_per_claim")),
        timeout=float(bounded(args.timeout, ABS_MAX_TIMEOUT_SECONDS, "timeout")),
        max_search_bytes=int(bounded(args.max_search_bytes, ABS_MAX_SEARCH_BYTES, "max_search_bytes")),
        max_fetch_bytes=int(bounded(args.max_fetch_bytes, ABS_MAX_FETCH_BYTES, "max_fetch_bytes")),
        max_excerpt_chars=int(bounded(args.max_excerpt_chars, ABS_MAX_EXCERPT_CHARS, "max_excerpt_chars")),
        max_redirects=int(bounded(args.max_redirects, ABS_MAX_REDIRECTS, "max_redirects")),
        allowlist=allowlist,
        frozen_searches=imported_searches,
        frozen_search_backend=imported_backend,
        frozen_search_cost_usd=imported_cost,
    )
    evidence["source_queue"] = file_record(queue_path)
    evidence["input_claims"] = dict(queue["input_record"])
    if search_results_path is not None:
        evidence["frozen_search_results"] = file_record(search_results_path)
    validate_evidence(evidence, require_payload=True, require_source_chain=True)
    output = (args.output or queue_path.parent / "source-evidence.json").resolve()
    write_json(output, evidence)
    print(json.dumps({"output": str(output), "claims": len(evidence["claims"]), **evidence["usage"], "budget_exceeded": evidence["budget_exceeded"]}, ensure_ascii=False))
    return 0


def validate_evidence(
    document: dict[str, Any],
    *,
    require_payload: bool = False,
    require_source_chain: bool = False,
) -> None:
    protocol = document.get("protocol") or {}
    if (
        protocol.get("version") != EVIDENCE_PROTOCOL
        or protocol.get("kind") != "claim-evidence"
        or protocol.get("mode") not in {"extract", "repair"}
    ):
        raise ValueError("evidence protocol/version/mode 非法")
    if not isinstance(document.get("claims"), list):
        raise ValueError("evidence.claims 必须是数组")
    usage = document.get("usage")
    limits = document.get("hard_limits")
    compliance = document.get("compliance")
    if not isinstance(usage, dict) or not isinstance(limits, dict) or not isinstance(compliance, dict):
        raise ValueError("evidence 必须包含 usage/hard_limits/compliance objects")
    if not isinstance(document.get("budget_exceeded"), bool):
        raise ValueError("evidence.budget_exceeded 必须是 boolean")
    if require_source_chain:
        queue_path = assert_file_record(document.get("source_queue") or {}, "source queue")
        queue = read_json(queue_path)
        if (queue.get("protocol") or {}).get("version") != QUEUE_PROTOCOL:
            raise ValueError("source queue protocol 非法")
        input_path = assert_file_record(queue.get("input_record") or {}, "frozen claims input")
        if document.get("input_claims") != queue.get("input_record"):
            raise ValueError("evidence input_claims 与 source queue 冻结输入不一致")
        input_claims, input_mode = frozen_claims(read_json(input_path))
        evidence_claims = [item.get("claim") for item in document.get("claims", [])]
        canonical = lambda value: json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        ordered = lambda rows: sorted(rows, key=lambda item: str((item or {}).get("claim_id", "")))
        if input_mode != protocol.get("mode") or canonical(ordered(input_claims)) != canonical(ordered(evidence_claims)):
            raise ValueError("evidence claims 不是 source queue 输入的精确派生")
        if document.get("frozen_search_results") is not None:
            search_path = assert_file_record(
                document.get("frozen_search_results") or {}, "frozen search results"
            )
            frozen_search_records(read_json(search_path), queue)
    for key in ("network_queries", "network_fetches", "cache_hits"):
        if not isinstance(usage.get(key), int) or usage[key] < 0:
            raise ValueError(f"evidence.usage.{key} 必须是非负整数")
    cost = usage.get("cost_usd")
    if not isinstance(cost, (int, float)) or isinstance(cost, bool) or not math.isfinite(float(cost)) or cost < 0:
        raise ValueError("evidence.usage.cost_usd 必须是显式非负有限数")
    for key in ("max_searches", "max_fetches", "max_candidates_per_claim"):
        if not isinstance(limits.get(key), int) or limits[key] < 0:
            raise ValueError(f"evidence.hard_limits.{key} 必须是非负整数")
    if usage["network_queries"] > limits["max_searches"] or usage["network_fetches"] > limits["max_fetches"]:
        raise ValueError("evidence usage 超过 hard_limits")
    if usage["cache_hits"] > limits["max_searches"] + limits["max_fetches"]:
        raise ValueError("evidence cache_hits 超过可缓存操作总数")
    if "search_cache_hits" in usage or "fetch_cache_hits" in usage:
        if usage.get("search_cache_hits", 0) + usage.get("fetch_cache_hits", 0) != usage["cache_hits"]:
            raise ValueError("evidence cache hit 分项与总数不一致")
    if not isinstance(compliance.get("all_evidence_compliant"), bool):
        raise ValueError("evidence.compliance 缺少 all_evidence_compliant boolean")
    total_candidates = 0
    claims_with_evidence = 0
    urls_compliant = 0
    quotes_compliant = 0
    hashes_compliant = 0
    for index, item in enumerate(document["claims"]):
        if not isinstance(item, dict) or not isinstance(item.get("claim"), dict) or not isinstance(item.get("candidates"), list):
            raise ValueError(f"evidence.claims[{index}] 非法")
        if len(item["candidates"]) > limits["max_candidates_per_claim"]:
            raise ValueError(f"evidence.claims[{index}] 候选数超过硬限制")
        total_candidates += len(item["candidates"])
        claims_with_evidence += bool(item["candidates"])
        for candidate in item["candidates"]:
            if not re.fullmatch(r"[0-9a-f]{64}", str(candidate.get("content_sha256", ""))):
                raise ValueError(f"evidence.claims[{index}] candidate 缺内容 hash")
            if not str(candidate.get("excerpt", "")).strip():
                raise ValueError(f"evidence.claims[{index}] candidate 缺逐字原文")
            if require_payload:
                payload_path = Path(str(candidate.get("payload_path", ""))).resolve()
                if not payload_path.exists() or not payload_path.is_file():
                    raise ValueError(f"evidence.claims[{index}] 缺可重放 raw payload")
                payload = payload_path.read_bytes()
                if sha256_bytes(payload) != candidate.get("content_sha256"):
                    raise ValueError(f"evidence.claims[{index}] raw payload hash 不匹配")
                if len(payload) != candidate.get("bytes"):
                    raise ValueError(f"evidence.claims[{index}] raw payload bytes 不匹配")
                max_excerpt_chars = limits.get("max_excerpt_chars")
                if not isinstance(max_excerpt_chars, int) or max_excerpt_chars <= 0:
                    raise ValueError("evidence hard_limits 缺 max_excerpt_chars，无法重放 excerpt")
                media_type, excerpt = extract_excerpt(
                    str(candidate.get("url", "")), payload,
                    str(candidate.get("content_type", "")), max_excerpt_chars,
                    str(candidate.get("excerpt_locator", "")),
                )
                if media_type != candidate.get("content_type") or excerpt != candidate.get("excerpt"):
                    raise ValueError(f"evidence.claims[{index}] raw payload excerpt 重放不一致")
            # DNS was already pinned during run; materialization still enforces
            # the non-local scheme/hostname layer without making network calls.
            parsed = urllib.parse.urlsplit(str(candidate.get("url", "")))
            host = (parsed.hostname or "").lower().rstrip(".")
            try:
                port = parsed.port
            except ValueError as exc:
                raise ValueError(f"evidence.claims[{index}] candidate URL 非法") from exc
            try:
                literal = ipaddress.ip_address(host.split("%", 1)[0])
            except ValueError:
                try:
                    literal = ipaddress.ip_address(socket.inet_aton(host))
                except OSError:
                    literal = None
            expected_port = 443 if parsed.scheme == "https" else 80
            if (
                parsed.scheme not in {"http", "https"}
                or not host
                or parsed.username is not None
                or parsed.password is not None
                or port not in {None, expected_port}
                or host == "localhost"
                or host.endswith(".localhost")
                or host.endswith(".local")
                or (literal is not None and not literal.is_global)
                or not domain_allowed(host, normalized_domains(limits.get("allowed_domains", [])))
            ):
                raise ValueError(f"evidence.claims[{index}] candidate URL 非法")
            urls_compliant += 1
            quotes_compliant += bool(str(candidate.get("excerpt", "")).strip())
            hashes_compliant += bool(re.fullmatch(r"[0-9a-f]{64}", str(candidate.get("content_sha256", ""))))

    expected_compliance = {
        "claims_total": len(document["claims"]),
        "claims_with_evidence": claims_with_evidence,
        "claims_without_evidence": len(document["claims"]) - claims_with_evidence,
        "candidates_returned": total_candidates,
        "http_urls_valid": urls_compliant,
        "urls_compliant": urls_compliant,
        "quotes_nonempty": quotes_compliant,
        "quotes_compliant": quotes_compliant,
        "content_hashes_valid": hashes_compliant,
        "content_hashes_compliant": hashes_compliant,
        "all_evidence_compliant": (
            urls_compliant == quotes_compliant == hashes_compliant == total_candidates
        ),
    }
    for key, expected in expected_compliance.items():
        if compliance.get(key) != expected:
            raise ValueError(f"evidence.compliance.{key} 账目不一致: {compliance.get(key)!r} != {expected!r}")
    if not isinstance(compliance.get("rejected_urls"), int) or compliance["rejected_urls"] < 0:
        raise ValueError("evidence.compliance.rejected_urls 必须是非负整数")


def materialize_workflow(evidence: dict[str, Any], template: Path, output: Path) -> None:
    validate_evidence(evidence)
    text = template.read_text(encoding="utf-8")
    start = "/* DATA-BINDING:START */"
    end = "/* DATA-BINDING:END */"
    if text.count(start) != 1 or text.count(end) != 1:
        raise ValueError("verify-only Workflow 缺少唯一 DATA-BINDING 标记")
    before, rest = text.split(start, 1)
    _, after = rest.split(end, 1)
    binding = f"\nconst EVIDENCE = {json.dumps(evidence, ensure_ascii=False, indent=2)}\n"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(before + start + binding + end + after, encoding="utf-8")


def command_materialize(args: argparse.Namespace) -> int:
    evidence_path = args.evidence.resolve()
    output = (args.output or evidence_path.parent / "verify-only.workflow.run.js").resolve()
    evidence = read_json(evidence_path)
    validate_evidence(evidence, require_payload=True, require_source_chain=True)
    materialize_workflow(evidence, args.template.resolve(), output)
    print(json.dumps({"output": str(output), "evidence": str(evidence_path), "web_search_allowed": False}, ensure_ascii=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    plan = sub.add_parser("plan", help="冻结 claim 级 source-first query queue")
    plan.add_argument("claims", type=Path)
    plan.add_argument("--max-claims", type=int, default=40)
    plan.add_argument("--max-searches", "--max-queries", dest="max_searches", type=int, default=20)
    plan.add_argument("--max-fetches", type=int, default=40)
    plan.add_argument("--max-candidates-per-claim", type=int, default=2)
    plan.add_argument(
        "--allow-domain", action="append", required=True,
        help="生产抓取域名白名单；可重复传入",
    )
    plan.add_argument("--output", type=Path)
    plan.set_defaults(handler=command_plan)

    run = sub.add_parser("run", help="执行确定性 Search/Fetch 并冻结 evidence")
    run.add_argument("queue", type=Path)
    run.add_argument("--max-searches", "--max-queries", dest="max_searches", type=int, default=20)
    run.add_argument("--max-fetches", type=int, default=40)
    run.add_argument("--max-candidates-per-claim", "--max-results", dest="max_candidates_per_claim", type=int, default=2)
    run.add_argument("--timeout", type=float, default=12.0)
    run.add_argument("--max-search-bytes", "--max-bytes", dest="max_search_bytes", type=int, default=1_500_000)
    run.add_argument("--max-fetch-bytes", type=int, default=8_000_000)
    run.add_argument("--max-excerpt-chars", "--sample-chars", dest="max_excerpt_chars", type=int, default=12_000)
    run.add_argument("--max-redirects", type=int, default=3)
    run.add_argument("--allow-domain", action="append")
    run.add_argument(
        "--search-results", type=Path,
        help="已计数并冻结的平台 Search 结果；提供后不会调用内置搜索后端",
    )
    run.add_argument(
        "--allow-synthetic-proxy-dns", action="store_true",
        help="显式允许受管桌面网络将已白名单域名解析到 198.18.0.0/15；字面 IP 仍拒绝",
    )
    run.add_argument("--cache-dir", type=Path, default=ROOT / "runs/wiki-search-cache")
    run.add_argument("--output", type=Path)
    run.set_defaults(handler=command_run)

    materialize = sub.add_parser("materialize", help="把 evidence 嵌入无 WebSearch 的 Verify-only Workflow")
    materialize.add_argument("evidence", type=Path)
    materialize.add_argument("--template", type=Path, default=ROOT / ".claude/workflows/wiki-ku-verify-only.js")
    materialize.add_argument("--output", type=Path)
    materialize.set_defaults(handler=command_materialize)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        return args.handler(args)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
