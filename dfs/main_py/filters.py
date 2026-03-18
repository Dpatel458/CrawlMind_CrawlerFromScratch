from urllib.parse import urlparse
import requests


class BaseFilter:
    def allow(self, url):
        raise NotImplementedError


# 1. Domain Filter
class DomainFilter(BaseFilter):
    def __init__(self, base_domain):
        self.base_domain = urlparse(base_domain).netloc

    def allow(self, url):
        return urlparse(url).netloc == self.base_domain


# 2. URL Pattern Filter (restrict path)
class URLPatternFilter(BaseFilter):
    def __init__(self, allowed_prefix):
        self.allowed_prefix = allowed_prefix.rstrip("/")

    def allow(self, url):
        return url.startswith(self.allowed_prefix)


# 3. External Link Filter
#
#    Blocks any URL whose domain differs from the base domain.
#    Use when you want to stay strictly within one site.
#    None = disabled (allow everything).
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
        netloc = urlparse(url).netloc.lower()
        # strip www. prefix before comparing
        netloc = netloc.removeprefix("www.")
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
        return True  # ambiguous → defer to Stage 2

    def allow_response(self, response):
        # Stage 2: real Content-Type from the fetched response
        content_type = response.headers.get("Content-Type", "")
        return any(t in content_type for t in self.allowed_types)



#
#    allow(url)          → run all filters' Stage 1 (pre-fetch, URL-only)
#    allow_response(...) → run filters that have Stage 2 (post-fetch)
#
class FilterChain:
    def __init__(self, filters, debug=False):
        self.filters = filters
        self.debug = debug

    def allow(self, url):
        for f in self.filters:
            result = f.allow(url)
            if self.debug:
                print(f"  [Filter: {f.__class__.__name__}] {url} → {result}")
            if not result:
                return False
        return True

    def allow_response(self, url, response):
        for f in self.filters:
            if not hasattr(f, "allow_response"):
                continue
            result = f.allow_response(response)
            if self.debug:
                print(f"  [Filter: {f.__class__.__name__}.allow_response] {url} → {result}")
            if not result:
                return False
        return True