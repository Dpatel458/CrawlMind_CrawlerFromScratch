import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse, urlunparse

from filters import (FilterChain, DomainFilter, URLPatternFilter,
                     ContentTypeFilter, ExternalLinkFilter, SocialMediaFilter)
from outputs import PageResult, OutputManager


def crawl(
    start_url,

    # ── output ─────────────────────────────────────────────────
    output_formats=None,        # list of str names or pre-configured BaseOutput instances
                                # e.g. ["markdown", "metadata"]
                                # e.g. [MarkdownOutput(strip_links=True), MetadataOutput()]
                                # e.g. ["json"]  → all formats, packed into to_json()
                                # None / []      → crawl only, no content stored

    # ── crawl controls ─────────────────────────────────────────
    max_depth=None,             # None = unlimited
    branching_factor=None,      # None = follow all links per page
    max_pages=None,             # None = no budget

    # ── content controls ───────────────────────────────────────
    score_threshold=None,       # None = off  |  e.g. 0.3
    word_count_threshold=None,  # None = off  |  e.g. 50

    # ── filter toggles ─────────────────────────────────────────
    url_pattern=None,           # None = off  |  e.g. "https://example.com/docs"
    exclude_external=False,
    exclude_social_media=False,
    extra_social_domains=None,

    # ── tracker ────────────────────────────────────────────────
    tracker=None,               # CrawlTracker | None = disabled

    # ── debug ──────────────────────────────────────────────────
    debug=False,
):
    """
    Single entry point for the DFS crawler.

    Parameters
    ----------
    start_url             : str
    output_formats        : list[str | BaseOutput]  (None = crawl only)
    max_depth             : int    (None = unlimited)
    branching_factor      : int    (None = all links)
    max_pages             : int    (None = unlimited)
    score_threshold       : float  (None = off)
    word_count_threshold  : int    (None = off)
    url_pattern           : str    (None = off)
    exclude_external      : bool
    exclude_social_media  : bool
    extra_social_domains  : list[str]
    debug                 : bool

    Returns
    -------
    list[PageResult]
    """

    # ── output manager ────────────────────────────────────────────────────────
    manager = OutputManager(output_formats or [])

    # ── filter chain ─────────────────────────────────────────────────────────
    active_filters = [DomainFilter(start_url)]

    if url_pattern is not None:
        active_filters.append(URLPatternFilter(url_pattern))
    if exclude_external:
        active_filters.append(ExternalLinkFilter(start_url))
    if exclude_social_media:
        active_filters.append(SocialMediaFilter(extra_domains=extra_social_domains))

    active_filters.append(ContentTypeFilter())

    filter_chain = FilterChain(active_filters, debug=debug)

    # ── crawl state ───────────────────────────────────────────────────────────
    visited    = set()   # canonical URLs already fetched
    page_count = [0]
    results    = []

    # ── helpers ───────────────────────────────────────────────────────────────

    def normalize_and_filter_url(current_url, href):
        absolute_url = urljoin(current_url, href)
        parsed       = urlparse(absolute_url)
        parsed       = parsed._replace(fragment="", query="")
        parsed       = parsed._replace(path=parsed.path.rstrip("/"))
        clean_url    = urlunparse(parsed)
        return clean_url if filter_chain.allow(clean_url) else None

    def score_link(href, anchor_text):
        score      = 0.0
        base_kw    = set(urlparse(start_url).path.strip("/").split("/")) - {""}
        link_parts = set(urlparse(href).path.strip("/").split("/"))      - {""}
        overlap    = len(base_kw & link_parts)
        score     += min(overlap / max(len(base_kw), 1), 1.0) * 0.5
        score     += 0.2 - min(len(link_parts) * 0.05, 0.2)
        if len(anchor_text.strip()) > 3:
            score += 0.3
        return round(min(score, 1.0), 3)

    # ── recursive worker ──────────────────────────────────────────────────────
    #
    # Design:
    #   - visited is the only global dedup set
    #   - page_count increments only after a real unique fetch
    #   - candidates are oversampled (branching_factor * 3) so that
    #     if some turn out to be already-visited by the time we recurse,
    #     we still have backups to fill the branching budget
    #   - redirect canonical URL is resolved before registering visited,
    #     so /tutorials/keras and /tutorials/keras/classification
    #     never both consume a page slot

    def _dfs_crawl(url, depth):

        url = url.rstrip("/")

        # ── pre-fetch guards ───────────────────────────────────────────────
        if url in visited:
            return
        if max_depth is not None and depth > max_depth:
            return
        if max_pages is not None and page_count[0] >= max_pages:
            print(f"[max_pages={max_pages} reached — stopping]")
            return

        indent   = "  " * depth
        page_num = f"[{page_count[0]+1}" + (f"/{max_pages}]" if max_pages else "]")
        print(f"\n{indent}Visiting {page_num}: {url}")

        result = PageResult(url=url, depth=depth, status_code=0)

        try:
            response           = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=5)
            result.status_code = response.status_code

            # ── resolve canonical URL after redirect ───────────────────────
            canonical = response.url.rstrip("/")
            if canonical != url:
                print(f"{indent}Redirected → {canonical}")
                if canonical in visited:
                    print(f"{indent}Skipped — canonical already visited")
                    return     # free — page_count not incremented yet

            # ── register as visited NOW (after redirect resolution) ────────
            visited.add(url)
            visited.add(canonical)
            page_count[0] += 1
            result.url = canonical

            if tracker:
                tracker.on_visit(canonical, depth)

            # ── content-type gate ──────────────────────────────────────────
            if not filter_chain.allow_response(url, response):
                result.error = f"Content-Type blocked: {response.headers.get('Content-Type', '?')}"
                print(f"{indent}Skipped — {result.error}")
                if tracker:
                    tracker.on_error(canonical, result.error)
                results.append(result)
                return

            # ── parse ──────────────────────────────────────────────────────
            raw_soup = BeautifulSoup(response.text, "html.parser")
            soup     = BeautifulSoup(response.text, "html.parser")

            NOISE_TAGS = ["script", "style", "nav", "header", "footer",
                          "aside", "form", "iframe", "noscript"]
            for tag in soup(NOISE_TAGS):
                tag.decompose()

            NOISE_PATTERNS = [
                "header", "footer", "nav", "cookie", "banner",
                "breadcrumb", "sidebar", "toc", "toolbar", "menu",
                "announcement", "notification", "skip", "search",
                "devsite-band", "devsite-collection", "devsite-rating",
                "devsite-thumb", "devsite-page-rating", "devsite-bookmark",
            ]
            for tag in list(soup.find_all(True)):
                if tag.parent is None:
                    continue
                combined = " ".join([
                    tag.name or "",
                    " ".join(tag.get("class") or []),
                    tag.get("id") or "",
                ]).lower()
                if any(p in combined for p in NOISE_PATTERNS):
                    tag.decompose()

            content = (
                soup.find("main")
                or soup.find("article")
                or soup.find(attrs={"role": "main"})
                or soup.find("body")
                or soup
            )

            # ── word count gate ────────────────────────────────────────────
            text = " ".join(content.get_text(" ", strip=True).split())
            if word_count_threshold is not None:
                wc = len(text.split())
                if wc < word_count_threshold:
                    result.error = f"word_count={wc} < threshold={word_count_threshold}"
                    print(f"{indent}Skipped — {result.error}")
                    if tracker:
                        tracker.on_error(canonical, result.error)
                    results.append(result)
                    return

            print(f"{indent}({len(text.split())} words): {text[:200]}...")

            if tracker:
                tracker.on_success(canonical, text)

            manager.extract_all(result, content, raw_soup, response)
            results.append(result)

            # ── collect and RESERVE children ───────────────────────────────
            #
            # Steps:
            #  1. Collect candidate URLs from this page's content
            #  2. Resolve redirects NOW (HEAD request) to get canonical URLs
            #     so /tutorials/keras and /tutorials/keras/classification
            #     are treated as the same URL before any reservation
            #  3. Reserve exactly branching_factor canonical URLs in visited
            #  4. Recurse — subtrees see reservations and cannot steal them
            #  5. Before each recursion, temporarily discard the reservation
            #     so _dfs_crawl can re-register it properly after fetch
            #
            limit         = branching_factor if branching_factor else None
            children      = []   # list of (original_url, canonical_url)
            seen_canonical = set()
            dropped_filt  = 0
            dropped_score = 0
            dropped_vis   = 0

            for tag in content.find_all("a", href=True):
                clean_url = normalize_and_filter_url(canonical, tag["href"])
                if not clean_url:
                    dropped_filt += 1
                    continue
                if clean_url == canonical:
                    continue

                # resolve redirect to get true canonical URL
                try:
                    head = requests.head(
                        clean_url,
                        headers={"User-Agent": "Mozilla/5.0"},
                        allow_redirects=True,
                        timeout=3,
                    )
                    true_canonical = head.url.rstrip("/")
                except Exception:
                    true_canonical = clean_url

                if true_canonical in seen_canonical or true_canonical in visited:
                    dropped_vis += 1
                    continue

                if score_threshold is not None:
                    s = score_link(clean_url, tag.get_text())
                    if s < score_threshold:
                        if debug:
                            print(f"{indent}  [score={s:.2f}] dropped: {clean_url}")
                        dropped_score += 1
                        continue

                seen_canonical.add(true_canonical)
                children.append((clean_url, true_canonical))

                if limit and len(children) >= limit:
                    break

            # reserve all canonical URLs before any recursion
            for _, canon in children:
                visited.add(canon)

            print(f"{indent}  children: {len(children)} reserved | "
                  f"already-visited: {dropped_vis} | "
                  f"score-filtered: {dropped_score} | "
                  f"url-filtered: {dropped_filt}")
            for orig, canon in children:
                print(f"{indent}  -> {orig}" + (f" → {canon}" if canon != orig else ""))

            # recurse — discard reservation temporarily so _dfs_crawl
            # can process each child normally through its own redirect logic
            for orig, canon in children:
                if max_pages and page_count[0] >= max_pages:
                    break
                visited.discard(orig)
                visited.discard(canon)
                _dfs_crawl(orig, depth + 1)

        except Exception as e:
            result.error = str(e)
            print(f"{indent}Error: {e}")
            if tracker:
                tracker.on_error(url, str(e))
            results.append(result)

    # ── kick off ──────────────────────────────────────────────────────────────
    _dfs_crawl(start_url, depth=0)
    successful = len([r for r in results if not r.error])
    print(f"\nDone — {page_count[0]} visited, {successful} successful.")

    if tracker and tracker.auto_save:
        tracker.save()

    return results


# ── Usage ──────────────────────────────────────────────────────────────────────

# if __name__ == "__main__":

#     from outputs import MarkdownOutput, MetadataOutput, LinksOutput

#     # 1. crawl only
#     crawl("https://www.tensorflow.org/tutorials")

#     # 2. string names — quick and simple
#     results = crawl(
#         start_url      = "https://www.tensorflow.org/tutorials",
#         output_formats = ["markdown", "metadata", "links"],
#         max_depth      = 1,
#         max_pages      = 5,
#     )

#     # 3. pre-configured instances — full control over each extractor
#     results = crawl(
#         start_url      = "https://www.tensorflow.org/tutorials",
#         output_formats = [
#             MarkdownOutput(heading_style="ATX", strip_links=True),
#             MetadataOutput(),
#             LinksOutput(include_external=False),
#         ],
#         max_depth      = 1,
#         max_pages      = 5,
#     )

#     # 4. json — all formats, one serialisable object per page
#     results = crawl(
#         start_url      = "https://www.tensorflow.org/tutorials",
#         output_formats = ["json"],
#         max_depth      = 1,
#         max_pages      = 3,
#     )
#     for r in results:
#         print(r.to_json())

#     # 5. fully controlled
#     results = crawl(
#         start_url            = "https://www.tensorflow.org/tutorials",
#         output_formats       = ["cleaned_html", "markdown", "metadata", "links"],
#         max_depth            = 2,
#         branching_factor     = 3,
#         max_pages            = 10,
#         score_threshold      = 0.3,
#         word_count_threshold = 50,
#         url_pattern          = "https://www.tensorflow.org/tutorials",
#         exclude_external     = True,
#         exclude_social_media = True,
#         debug                = False,
#     )
#     for r in results:
#         print(f"\n{'='*60}")
#         print(f"URL:    {r.url}  |  depth={r.depth}  |  status={r.status_code}")
#         if r.metadata:
#             print(f"Title:  {r.metadata['title']}")
#             print(f"Words:  {r.metadata['word_count']}")
#         if r.links:
#             print(f"Links:  {len(r.links)} found")
#         if r.markdown:
#             print(f"MD preview:\n{r.markdown[:400]}")