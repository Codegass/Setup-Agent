"""Web search tool for finding information online."""

from html.parser import HTMLParser
from typing import Any, Dict, List
from urllib.parse import urljoin, urlsplit

import requests
from loguru import logger

from ..base import BaseTool, ToolResult


class _PageText(HTMLParser):
    """Readable page text with source links; never execute page content."""

    def __init__(self, url: str):
        super().__init__()
        self.url = url
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.hidden += 1
        if not self.hidden:
            if tag in ("p", "div", "li", "pre", "br", "h1", "h2", "h3", "tr"):
                self.parts.append("\n")
            if tag == "a":
                href = dict(attrs).get("href")
                if href:
                    link = urljoin(self.url, href)
                    if urlsplit(link).scheme in ("http", "https"):
                        self.parts.append(f" [{link}] ")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self.hidden:
            self.hidden -= 1
        elif tag in ("p", "div", "li", "pre", "h1", "h2", "h3", "tr"):
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


class WebSearchTool(BaseTool):
    """Tool for searching the web for information."""

    def __init__(self):
        super().__init__(
            name="web_search",
            description="Search the web for information about errors, documentation, "
            "installation instructions, or any other information needed for setup.",
        )

    def execute(self, query: str, max_results: int = 5) -> ToolResult:
        """Execute a web search."""
        # The base class now handles parameter validation automatically

        if not query.strip():
            from ..base import ToolError

            raise ToolError(
                message="Empty search query provided",
                category="validation",
                error_code="EMPTY_QUERY",
                suggestions=["Provide a non-empty search query"],
                retryable=True,
            )

        logger.debug(f"Searching web for: {query}")

        try:
            # Use DuckDuckGo instant answer API for simple searches
            results = self._search_duckduckgo(query, max_results)

            if not results:
                return ToolResult.completed(
                    operation_outcome="unknown",
                    evidence_status="unknown",
                    output="DuckDuckGo Instant Answer returned no usable sources. This endpoint is not a full web index.",
                    error="Search provider returned no usable source evidence",
                    error_code="WEB_SEARCH_NO_EVIDENCE",
                    metadata={"query": query, "results_count": 0},
                    facts={
                        "provider": "duckduckgo_instant_answer",
                        "page_reader": "search(target='url:<https URL>')",
                    },
                )

            # Format results
            output = f"Search results for '{query}':\n\n"
            for i, result in enumerate(results, 1):
                output += f"{i}. {result['title']}\n"
                output += f"   {result['url']}\n"
                output += f"   {result['snippet']}\n\n"

            return ToolResult.completed_success(
                output=output,
                metadata={"query": query, "results_count": len(results), "results": results},
            )

        except Exception as e:
            error_msg = f"Web search failed: {str(e)}"
            logger.error(f"Web search error for query '{query}': {error_msg}")
            return ToolResult.completed_failure(
                output="",
                error=error_msg,
                error_code="WEB_SEARCH_UNAVAILABLE",
                metadata={"query": query},
                facts={
                    "provider": "duckduckgo_instant_answer",
                    "page_reader": "search(target='url:<https URL>')",
                },
            )

    def _search_duckduckgo(self, query: str, max_results: int) -> List[Dict[str, str]]:
        """Search using DuckDuckGo API."""
        try:
            # DuckDuckGo instant answer API
            url = "https://api.duckduckgo.com/"
            params = {"q": query, "format": "json", "no_html": "1", "skip_disambig": "1"}

            response = requests.get(url, params=params, timeout=10)
            response.raise_for_status()

            data = response.json()
            results = []

            # Try to get results from different sections
            # Abstract
            if data.get("Abstract"):
                results.append(
                    {
                        "title": data.get("AbstractText", "")[:100] + "...",
                        "url": data.get("AbstractURL", ""),
                        "snippet": data.get("Abstract", "")[:300] + "...",
                    }
                )

            # Related topics
            for topic in data.get("RelatedTopics", [])[:max_results]:
                if isinstance(topic, dict) and topic.get("Text"):
                    results.append(
                        {
                            "title": topic.get("Text", "")[:100] + "...",
                            "url": topic.get("FirstURL", ""),
                            "snippet": topic.get("Text", "")[:300] + "...",
                        }
                    )

            return results[:max_results]

        except Exception as e:
            logger.warning(f"DuckDuckGo search failed: {e}")
            raise

    def read_url(self, url: str) -> ToolResult:
        """Read a known documentation/release URL; large output uses normal storage."""
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            return ToolResult.completed_failure(
                output="",
                error="Page reads require an HTTPS URL without credentials",
                error_code="WEB_URL_INVALID",
            )
        try:
            with requests.get(url, timeout=(10, 30), stream=True) as response:
                response.raise_for_status()
                content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
                if content_type not in (
                    "text/html",
                    "text/plain",
                    "application/json",
                    "application/xhtml+xml",
                    "text/markdown",
                ):
                    return ToolResult.completed_failure(
                        output="",
                        error=f"Unsupported page content type: {content_type}",
                        error_code="WEB_CONTENT_UNSUPPORTED",
                        facts={"url": url, "content_type": content_type},
                    )
                data = bytearray()
                for chunk in response.iter_content(chunk_size=65536):
                    data.extend(chunk)
                    if len(data) > 4 * 1024 * 1024:
                        return ToolResult.completed_failure(
                            output="",
                            error="Page exceeds the 4 MiB fetch limit; no complete page was read",
                            error_code="WEB_PAGE_TOO_LARGE",
                            facts={"url": url, "complete": False},
                        )
                body = bytes(data).decode(response.encoding or "utf-8", errors="replace")
                if content_type in ("text/html", "application/xhtml+xml"):
                    parser = _PageText(response.url)
                    parser.feed(body)
                    body = "\n".join(
                        line.strip() for line in "".join(parser.parts).splitlines() if line.strip()
                    )
                return ToolResult.completed_success(
                    output=f"Source: {response.url}\n\n{body}",
                    facts={
                        "requested_url": url,
                        "source_url": response.url,
                        "http_status": response.status_code,
                        "bytes": len(data),
                        "complete": True,
                    },
                )
        except requests.RequestException as exc:
            return ToolResult.completed_failure(
                output="",
                error=f"Page read failed: {exc}",
                error_code="WEB_PAGE_UNAVAILABLE",
                facts={"url": url, "complete": False},
            )

    def _get_parameters_schema(self) -> Dict[str, Any]:
        """Get the parameters schema for this tool."""
        return {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The search query"},
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of results to return (default: 5)",
                    "default": 5,
                    "minimum": 1,
                    "maximum": 10,
                },
            },
            "required": ["query"],
        }
