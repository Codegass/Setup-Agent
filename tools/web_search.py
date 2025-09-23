"""Web search tool for finding information online."""

import json
from typing import Any, Dict, List
from urllib.parse import quote

import requests
from loguru import logger

from .base import BaseTool, ToolResult


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
            from .base import ToolError
            raise ToolError(
                message="Empty search query provided",
                category="validation",
                error_code="EMPTY_QUERY",
                suggestions=["Provide a non-empty search query"],
                retryable=True
            )

        logger.debug(f"Searching web for: {query}")

        try:
            # Use DuckDuckGo instant answer API for simple searches
            results = self._search_duckduckgo(query, max_results)

            if not results:
                return ToolResult(
                    success=True,
                    output="No search results found for the query.",
                    metadata={"query": query, "results_count": 0},
                )

            # Format results
            output = f"Search results for '{query}':\n\n"
            for i, result in enumerate(results, 1):
                output += f"{i}. {result['title']}\n"
                output += f"   {result['url']}\n"
                output += f"   {result['snippet']}\n\n"

            return ToolResult(
                success=True,
                output=output,
                metadata={"query": query, "results_count": len(results), "results": results},
            )

        except Exception as e:
            error_msg = f"Web search failed: {str(e)}"
            logger.error(f"Web search error for query '{query}': {error_msg}")
            return ToolResult(success=False, output="", error=error_msg, metadata={"query": query})

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

            # If no results from DuckDuckGo, try a simple search
            if not results:
                results = self._fallback_search(query, max_results)

            return results[:max_results]

        except Exception as e:
            logger.warning(f"DuckDuckGo search failed: {e}, trying fallback")
            return self._fallback_search(query, max_results)

    def _fallback_search(self, query: str, max_results: int) -> List[Dict[str, str]]:
        """Fallback search method."""
        # For now, return a message indicating web search is limited
        # In a production environment, you might integrate with other APIs
        return [
            {
                "title": "Web Search Limited",
                "url": f"https://www.google.com/search?q={quote(query)}",
                "snippet": (
                    f"Direct web search is limited in this environment. "
                    f"You can manually search for '{query}' using the URL above. "
                    f"Consider checking official documentation, GitHub issues, "
                    f"or Stack Overflow for more information."
                ),
            }
        ]

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
