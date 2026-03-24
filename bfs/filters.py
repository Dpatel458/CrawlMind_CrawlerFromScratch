from abc import ABC, abstractmethod
from urllib.parse import urlparse


class BaseFilter(ABC):
    @abstractmethod
    def allow(self, url: str) -> bool: ...


# 1. Domain Filter
class DomainFilter(BaseFilter):
    def __init__(self, base_domain):
        self.base_domain = urlparse(base_domain).netloc

    def allow(self, url):
        return urlparse(url).netloc == self.base_domain


# 2. URL Pattern Filter (restrict to a path prefix)
#
#    Matches on the URL path only, ignoring query/fragment.
#    This avoids false negatives when query parameters appear
#    before the pattern would otherwise match.
#
class URLPatternFilter(BaseFilter):
    def __init__(self, allowed_prefix):
        parsed = urlparse(allowed_prefix)
        self.allowed_netloc = parsed.netloc
        self.allowed_path   = parsed.path.rstrip("/")

    def allow(self, url):
        parsed = urlparse(url)
        # netloc must match AND path must start with the allowed prefix
        return (
            parsed.netloc == self.allowed_netloc
            and parsed.path.rstrip("/").startswith(self.allowed_path)
        )


# 3. External Link Filter
#
#    Blocks any URL whose domain differs from the base domain.
#    Use when you want to stay strictly within one site.
#
#    NOTE: DomainFilter already enforces same-domain crawling at the
#    filter-chain level. ExternalLinkFilter exists as a standalone
#    opt-in filter for cases where DomainFilter is not in the chain.
#    In crawler1.py the chain always starts with DomainFilter, so
#    ExternalLinkFilter is intentionally NOT added when exclude_external
#    is True — that would be a duplicate check.
#
class ExternalLinkFilter(BaseFilter):
    def __init__(self, base_url):
        self.base_domain = urlparse(base_url).netloc

    def allow(self, url):
        return urlparse(url).netloc == self.base_domain


# 4. Social Media Filter
#
#    Blocks links to known social media / sharing domains.
#    Extend SOCIAL_DOMAINS to add more.
#
SOCIAL_DOMAINS = {
    "twitter.com", "x.com",
    "facebook.com", "fb.com",
    "instagram.com",
    "linkedin.com",
    "youtube.com", "youtu.be",
    "tiktok.com",
    "reddit.com",
    "pinterest.com",
    "snapchat.com",
    "t.me", "telegram.org",
    "wa.me", "whatsapp.com",
}

class SocialMediaFilter(BaseFilter):
    def __init__(self, extra_domains=None):
        self.blocked = SOCIAL_DOMAINS | set(extra_domains or [])

    def allow(self, url):
        netloc = urlparse(url).netloc.lower().removeprefix("www.")
        return netloc not in self.blocked


# 5. Content Type Filter
#
#    Two-stage design:
#
#    Stage 1 — allow(url)
#      Called BEFORE fetching, on candidate links.
#      Only does a fast extension check — no network call.
#      Blocks obvious non-HTML files (.pdf, .jpg, etc.)
#      Returns True for anything ambiguous (let it through to Stage 2).
#
#    Stage 2 — allow_response(response)
#      Called AFTER fetching, on the actual HTTP response.
#      Checks the real Content-Type header — zero extra requests.
#      This is where ambiguous URLs get correctly classified.
#
class ContentTypeFilter(BaseFilter):
    def __init__(self, allowed_types=("text/html",)):
        self.allowed_types = allowed_types
        self.blocked_extensions = (".pdf", ".jpg", ".jpeg", ".png", ".gif",
                                   ".zip", ".gz", ".tar", ".mp4", ".mp3",
                                   ".svg", ".ico", ".woff", ".woff2")

    def allow(self, url):
        # Stage 1: extension check only — no HEAD request
        if url.lower().endswith(self.blocked_extensions):
            return False
        return True  # ambiguous -> defer to Stage 2

    def allow_response(self, response):
        # Stage 2: real Content-Type from the fetched response
        content_type = response.headers.get("Content-Type", "")
        return any(t in content_type for t in self.allowed_types)


# 6. Robots.txt Filter
#
#    Respects robots.txt exclusion rules for a given user-agent.
#    Fetches and caches robots.txt once per domain.
#    Paths listed under Disallow are blocked; Allow overrides Disallow.
#
#    Parameters
#    ----------
#    user_agent : str  — the User-Agent string to check rules for.
#                        Falls back to "*" wildcard rules if no specific
#                        rules exist for the given agent. Default: "*".
#
class RobotsTxtFilter(BaseFilter):
    def __init__(self, user_agent="*"):
        self.user_agent = user_agent
        self._cache     = {}   # netloc -> urllib.robotparser.RobotFileParser

    def _get_parser(self, url):
        from urllib.robotparser import RobotFileParser
        import requests as _req

        parsed  = urlparse(url)
        netloc  = parsed.netloc
        if netloc in self._cache:
            return self._cache[netloc]

        robots_url = f"{parsed.scheme}://{netloc}/robots.txt"
        parser     = RobotFileParser()
        parser.set_url(robots_url)
        try:
            resp = _req.get(robots_url, timeout=5, headers={"User-Agent": "Mozilla/5.0"})
            if resp.status_code == 200:
                parser.parse(resp.text.splitlines())
            # if 404 or error — treat as allow-all (no rules)
        except Exception:
            pass   # network error -> allow-all

        self._cache[netloc] = parser
        return parser

    def allow(self, url):
        parser = self._get_parser(url)
        return parser.can_fetch(self.user_agent, url)


# -- FilterChain ---------------------------------------------------------------
#
#    allow(url)          -> run all filters' Stage 1 (pre-fetch, URL-only)
#    allow_response(...) -> run filters that have Stage 2 (post-fetch)
#
class FilterChain:
    def __init__(self, filters, debug=False):
        self.filters = filters
        self.debug   = debug

    def allow(self, url):
        for f in self.filters:
            result = f.allow(url)
            if self.debug:
                print(f"  [Filter: {f.__class__.__name__}] {url} -> {result}")
            if not result:
                return False
        return True

    def allow_response(self, url, response):
        for f in self.filters:
            if not hasattr(f, "allow_response"):
                continue
            result = f.allow_response(response)
            if self.debug:
                print(f"  [Filter: {f.__class__.__name__}.allow_response] {url} -> {result}")
            if not result:
                return False
        return True
