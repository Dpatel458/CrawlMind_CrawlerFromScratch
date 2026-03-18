import re
import json
from dataclasses import dataclass
from typing import Optional
from markdownify import markdownify


# ── PageResult ─────────────────────────────────────────────────────────────────

@dataclass
class PageResult:
    url:          str
    depth:        int
    status_code:  int
    error:        Optional[str]  = None

    cleaned_html: Optional[str]  = None
    raw_html:     Optional[str]  = None
    markdown:     Optional[str]  = None
    metadata:     Optional[dict] = None
    links:        Optional[list] = None

    def to_json(self) -> str:
        return json.dumps({
            "url":          self.url,
            "depth":        self.depth,
            "status_code":  self.status_code,
            "error":        self.error,
            "cleaned_html": self.cleaned_html,
            "raw_html":     self.raw_html,
            "markdown":     self.markdown,
            "metadata":     self.metadata,
            "links":        self.links,
        }, indent=2, ensure_ascii=False)


# ── Base output class ──────────────────────────────────────────────────────────
#
#   extract(content, raw_soup, response)
#
#   content  — noise-stripped, scoped to <main>/<article> — use for text/markdown
#   raw_soup — full untouched parse               — use for <head> meta tags, raw html
#   response — the requests.Response object       — use for headers, status, raw text
#
class BaseOutput:
    name: str = ""

    def extract(self, content, raw_soup, response):
        raise NotImplementedError


# ── Output classes ─────────────────────────────────────────────────────────────

class CleanedHtmlOutput(BaseOutput):
    """
    Scoped, noise-stripped HTML — everything outside <main>/<article> removed.
    Good for re-parsing downstream or feeding to an LLM.
    """
    name = "cleaned_html"

    def extract(self, content, raw_soup, response):
        return str(content)


class RawHtmlOutput(BaseOutput):
    """
    response.text exactly as received — zero modifications.
    """
    name = "raw_html"

    def extract(self, content, raw_soup, response):
        return response.text


class MarkdownOutput(BaseOutput):
    """
    Main content converted to clean Markdown.

    Parameters
    ----------
    heading_style : "ATX" (# headings) or "SETEX" (underline). Default: "ATX".
    strip_links   : Keep anchor text, drop URLs. Default: False.
    strip_images  : Remove <img> tags entirely. Default: False.
    """
    name = "markdown"

    # Tags that are never meaningful in Markdown output —
    # button rows, notebook action bars, feedback widgets, etc.
    _STRIP_TAGS = [
        "button", "devsite-colab", "devsite-page-rating",
        "devsite-bookmark", "devsite-thumb",
        "[document]",   # markdownify internal — strips the root document node text
    ]

    def __init__(self, heading_style="ATX", strip_links=False, strip_images=False):
        self.heading_style = heading_style
        self.strip = list(self._STRIP_TAGS)
        if strip_links:
            self.strip.append("a")
        if strip_images:
            self.strip.append("img")

    def extract(self, content, raw_soup, response):
        import copy
        # work on a copy so we don't mutate the shared content soup
        soup = copy.copy(content)

        # remove button-bar rows before converting
        # these are <p> or <div> tags that contain only action links
        # e.g. "View on TensorFlow.org | Run in Google Colab | ..."
        ACTION_TEXTS = {"view on tensorflow.org", "run in google colab",
                        "view source on github", "download notebook"}
        for tag in soup.find_all(["p", "div", "td"]):
            texts = {a.get_text(strip=True).lower() for a in tag.find_all("a")}
            if texts and texts.issubset(ACTION_TEXTS):
                tag.decompose()

        md = markdownify(
            str(soup),
            heading_style = self.heading_style,
            strip         = self.strip,
        )

        # collapse 3+ consecutive blank lines → single blank line
        md = re.sub(r"\n{3,}", "\n\n", md)

        return md.strip()


class MetadataOutput(BaseOutput):
    """
    Structured page metadata.

    Meta tags (title, description, og:*) come from raw_soup <head>.
    word_count comes from content (scoped, cleaned) — accurate, no nav inflation.

    Fields:
      title, description, og_title, og_desc, og_image,
      canonical, word_count, status_code, content_type
    """
    name = "metadata"

    def extract(self, content, raw_soup, response):
        def _meta(name=None, prop=None):
            tag = (raw_soup.find("meta", attrs={"name": name}) if name
                   else raw_soup.find("meta", attrs={"property": prop}))
            return tag["content"].strip() if tag and tag.get("content") else None

        canonical_tag = raw_soup.find("link", rel="canonical")

        # word_count from content (scoped clean soup) — not raw_soup
        word_count = len(content.get_text(" ", strip=True).split())

        return {
            "title":        raw_soup.title.string.strip() if raw_soup.title else None,
            "description":  _meta(name="description"),
            "og_title":     _meta(prop="og:title"),
            "og_desc":      _meta(prop="og:description"),
            "og_image":     _meta(prop="og:image"),
            "canonical":    canonical_tag["href"] if canonical_tag else None,
            "word_count":   word_count,
            "status_code":  response.status_code,
            "content_type": response.headers.get("Content-Type", ""),
        }


class LinksOutput(BaseOutput):
    """
    All outgoing <a href> links on the page.

    Returns: [{"href": ..., "text": ..., "internal": bool}, ...]

    Parameters
    ----------
    include_internal : Include same-domain links. Default: True.
    include_external : Include off-domain links.  Default: True.
    base_domain      : Set automatically by OutputManager from start_url.
    """
    name = "links"

    def __init__(self, include_internal=True, include_external=True, base_domain=None):
        self.include_internal = include_internal
        self.include_external = include_external
        self.base_domain      = base_domain

    def extract(self, content, raw_soup, response):
        from urllib.parse import urlparse
        links = []
        for tag in content.find_all("a", href=True):
            href = tag["href"].strip()
            text = tag.get_text(strip=True)
            if not href or href.startswith(("#", "javascript:")):
                continue
            netloc      = urlparse(href).netloc
            is_internal = netloc in ("", self.base_domain or "")
            if is_internal and not self.include_internal:
                continue
            if not is_internal and not self.include_external:
                continue
            links.append({
                "href":     href,
                "text":     text,
                "internal": is_internal,
            })
        return links


# ── OutputManager ──────────────────────────────────────────────────────────────

class OutputManager:

    _REGISTRY = {
        "cleaned_html": CleanedHtmlOutput,
        "raw_html":     RawHtmlOutput,
        "markdown":     MarkdownOutput,
        "metadata":     MetadataOutput,
        "links":        LinksOutput,
    }

    VALID_FORMATS = set(_REGISTRY.keys()) | {"json"}

    def __init__(self, outputs):
        """
        outputs : list of str names or pre-configured BaseOutput instances.

        Examples
        --------
        OutputManager(["markdown", "metadata"])
        OutputManager([MarkdownOutput(strip_links=True), MetadataOutput()])
        OutputManager(["json"])   # activates all formats
        """
        self.want_json = False
        self.outputs   = []

        for o in outputs:
            if isinstance(o, str):
                if o == "json":
                    self.want_json = True
                    continue
                if o not in self._REGISTRY:
                    raise ValueError(
                        f"Unknown format '{o}'. Valid: {self.VALID_FORMATS}"
                    )
                self.outputs.append(self._REGISTRY[o]())
            elif isinstance(o, BaseOutput):
                self.outputs.append(o)
            else:
                raise TypeError(f"Expected str or BaseOutput instance, got {type(o)}")

        if self.want_json and not self.outputs:
            self.outputs = [cls() for cls in self._REGISTRY.values()]

    def extract_all(self, result: PageResult, content, raw_soup, response):
        """
        Run every active extractor and write results onto PageResult.

        content  — scoped, noise-stripped soup (<main> or <article>)
        raw_soup — full untouched parse
        response — requests.Response
        """
        for output in self.outputs:
            try:
                value = output.extract(content, raw_soup, response)
                setattr(result, output.name, value)
            except Exception as ex:
                print(f"  [output:{output.name}] extraction failed: {ex}")