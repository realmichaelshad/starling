"""Authored sources for the world service.

Every URL, query and host below is written here by hand. Nothing is derived from a customer: the
engine never sends themes, handles or post text (docs/architecture/news-pool-shape.md).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FeedSource:
    name: str
    url: str


# Publisher RSS and Atom feeds in the lane the product writes about.
RSS_FEEDS: tuple[FeedSource, ...] = (
    FeedSource("TechCrunch AI", "https://techcrunch.com/category/artificial-intelligence/feed/"),
    FeedSource("VentureBeat AI", "https://venturebeat.com/category/ai/feed/"),
    FeedSource("Ars Technica", "https://feeds.arstechnica.com/arstechnica/technology-lab"),
    FeedSource("The Verge AI", "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml"),
    FeedSource("Hugging Face", "https://huggingface.co/blog/feed.xml"),
    FeedSource("Simon Willison", "https://simonwillison.net/atom/everything/"),
    FeedSource("Google DeepMind", "https://deepmind.google/blog/rss.xml"),
    FeedSource("OpenAI News", "https://openai.com/news/rss.xml"),
    FeedSource("The Decoder", "https://the-decoder.com/feed/"),
    FeedSource("Together AI", "https://www.together.ai/blog/rss.xml"),
    FeedSource("Google AI Blog", "https://blog.google/technology/ai/rss/"),
    FeedSource("Mistral", "https://mistral.ai/rss.xml"),
    FeedSource("Qwen", "https://qwenlm.github.io/blog/index.xml"),
)
FEED_ITEMS_PER_SOURCE = 8


@dataclass(frozen=True)
class AuthoredQuery:
    """A summary topic. `id` is what the engine sends; `topic` is the Hacker News search string."""

    id: str
    topic: str
    keywords: tuple[str, ...]


# Ids and topics match the engine's AUTHORED_QUERIES (news-pool-shape.md, "POST /v1/summary").
AUTHORED_QUERIES: tuple[AuthoredQuery, ...] = (
    AuthoredQuery(
        "ai-model-releases",
        "AI model release",
        ("model release", "releases", "launch", "new model", "open-weight", "gpt", "gemini", "llama", "qwen", "deepseek"),
    ),
    AuthoredQuery(
        "coding-agents",
        "coding agent",
        ("coding agent", "coding assistant", "agentic coding", "code review", "copilot", "cursor", "codex"),
    ),
    AuthoredQuery(
        "llm-pricing",
        "LLM pricing",
        ("pricing", "price", "per million tokens", "cheaper", "discount", "cost per"),
    ),
    AuthoredQuery(
        "ai-developer-tools",
        "developer tools AI",
        ("developer tool", "developers", "sdk", "api", "framework", "open source library", "cli"),
    ),
    AuthoredQuery(
        "open-weight-models",
        "open source model",
        ("open source model", "open-source model", "open-weight", "open weights", "apache 2.0", "hugging face"),
    ),
)
QUERIES_BY_ID: dict[str, AuthoredQuery] = {query.id: query for query in AUTHORED_QUERIES}

# Hacker News (Algolia API, keyless).
HN_SOURCE = "Hacker News"
HN_SEARCH_BY_DATE_URL = "https://hn.algolia.com/api/v1/search_by_date"
HN_SEARCH_URL = "https://hn.algolia.com/api/v1/search"
HN_ITEM_URL = "https://news.ycombinator.com/item?id={}"
HN_PER_QUERY = 5
HN_QUERY_MAX_AGE_S = 2 * 86_400

# Hacker News "host-hot": recent, well-discussed stories published on hosts that only cover the lane.
HN_HOST_HOT_HITS = 100
HN_HOST_HOT_MIN_POINTS = 50
HN_HOST_HOT_MAX_AGE_S = 48 * 3_600
HN_HOST_HOT_LIMIT = 12
LANE_HOSTS: tuple[str, ...] = (
    "openrouter.ai",
    "anthropic.com",
    "openai.com",
    "deepmind.google",
    "simonwillison.net",
    "huggingface.co",
    "cursor.com",
    "github.blog",
)
LANE_REPO_PREFIXES: tuple[str, ...] = (
    "github.com/anthropics/",
    "github.com/openai/",
    "github.com/sst/opencode",
    "github.com/google-gemini/",
    "github.com/cline/",
    "github.com/berriai/",
)

# OpenRouter public model list (keyless): models listed in the last 48 hours.
OPENROUTER_SOURCE = "OpenRouter"
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
OPENROUTER_MAX_AGE_S = 48 * 3_600
OPENROUTER_MIN_DESCRIPTION = 120
OPENROUTER_LIMIT = 12
LANE_FRONTIER = "frontier models"
# Labs whose new OpenRouter listing is news the day it appears (T2 "breaking"): model id prefixes.
WATCHED_LAB_PREFIXES: tuple[str, ...] = (
    "openai/", "anthropic/", "google/", "meta-llama/", "mistralai/", "deepseek/", "qwen/",
    "x-ai/", "moonshotai/", "z-ai/", "minimax/", "nvidia/", "microsoft/", "cohere/",
)
