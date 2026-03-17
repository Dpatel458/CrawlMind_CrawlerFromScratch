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


# 3. Content Type Filter (based on extension OR HEAD request)
class ContentTypeFilter(BaseFilter):
    def __init__(self, allowed_types=("text/html",)):
        self.allowed_types = allowed_types
        self.blocked_extensions = (".pdf", ".jpg", ".png", ".zip")

    def allow(self, url):
        # Fast check (extension)
        if url.endswith(self.blocked_extensions):
            return False

        # Optional: check via HEAD request (slower but accurate)
        try:
            response = requests.head(url, timeout=3)
            content_type = response.headers.get("Content-Type", "")
            return any(t in content_type for t in self.allowed_types)
        except:
            return False


# 4. Filter Chain
class FilterChain:
    def __init__(self, filters, debug=False):
        self.filters = filters
        self.debug = debug

    def allow(self, url):
        for f in self.filters:
            result = f.allow(url)

            if self.debug:
                print(f"[Filter: {f.__class__.__name__}] {url} → {result}")

            if not result:
                return False
        return True