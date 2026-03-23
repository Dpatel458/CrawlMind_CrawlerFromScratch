import sys
import time
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse, urlunparse

from filters import (FilterChain, DomainFilter, URLPatternFilter,
                     ContentTypeFilter, SocialMediaFilter, RobotsTxtFilter)
from outputs import PageResult, OutputManager


def crawl(
    start_url,

    # -- output ----------------------------------------------------------------
    output_formats=None,        # list of str names or pre-configured BaseOutput instances
                                # e.g. ["markdown", "metadata"]
                                # e.g. [MarkdownOutput(strip_links=True), MetadataOutput()]
                                # e.g. ["json"]  -> all formats, packed into to_json()
                                # None / []      -> crawl only, no content stored

    # -- crawl controls --------------------------------------------------------
    max_depth=None,             # None = unlimited
    branching_factor=None,      # None = follow all links per page
    max_pages=None,             # None = no budget

    # -- content controls ------------------------------------------------------
    score_threshold=None,       # None = off  |  e.g. 0.3
    word_count_threshold=None,  # None = off  |  e.g. 50

    # -- filter toggles --------------------------------------------------------
    url_pattern=None,           # None = off  |  e.g. "https://example.com/docs"
    exclude_external=False,     # enforced by DomainFilter which is always active;
                                # ExternalLinkFilter is NOT added (would be duplicate)
    exclude_social_media=False,
    extra_social_domains=None,
    respect_robots_txt=False,   # if True, obeys robots.txt rules for the domain

    # -- politeness ------------------------------------------------------------
    politeness_delay=0.0,       # seconds to wait between requests (0 = off)

    # -- tracker ---------------------------------------------------------------
    tracker=None,               # CrawlTracker | None = disabled

    # -- debug -----------------------------------------------------------------
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
    exclude_external      : bool   (DomainFilter always active — this is a no-op
                                    kept for API compatibility)
    exclude_social_media  : bool
    extra_social_domains  : list[str]
    respect_robots_txt    : bool
    politeness_delay      : float  seconds between requests
    debug                 : bool

    Returns
    -------
    list[PageResult]
    """

    # -- output manager --------------------------------------------------------
    # Pass start_url so OutputManager can set base_domain on LinksOutput
    manager = OutputManager(output_formats or [], start_url=start_url)

    # -- filter chain ----------------------------------------------------------
    # DomainFilter is always first — it enforces same-domain crawling.
    # ExternalLinkFilter is intentionally omitted: it does the same thing
    # and adding it when exclude_external=True would be a redundant check.
    active_filters = [DomainFilter(start_url)]

    if url_pattern is not None:
        active_filters.append(URLPatternFilter(url_pattern))
    if exclude_social_media:
        active_filters.append(SocialMediaFilter(extra_domains=extra_social_domains))
    if respect_robots_txt:
        active_filters.append(RobotsTxtFilter())

    active_filters.append(ContentTypeFilter())

    filter_chain = FilterChain(active_filters, debug=debug)

    # -- crawl state -----------------------------------------------------------
    visited    = set()   # URLs actually fetched — permanent, never removed
    reserved   = set()   # URLs claimed by a parent — prevents siblings from
                         # stealing a parent's chosen children before recursing
    page_count = 0
    results    = []

    # -- recursion depth guard -------------------------------------------------
    # Python's default recursion limit is 1000. At max_depth=None on a deep
    # site this would eventually raise RecursionError. We cap at a safe limit.
    # max_depth takes priority; if unset we cap at 50 levels.
    _MAX_SAFE_DEPTH = 50
    effective_max_depth = max_depth if max_depth is not None else _MAX_SAFE_DEPTH

    # -- helpers ---------------------------------------------------------------

    def normalize_and_filter_url(current_url, href):
        base = current_url if current_url.endswith("/") else current_url + "/"
        absolute_url = urljoin(base, href)
        parsed       = urlparse(absolute_url)
        parsed       = parsed._replace(fragment="", query="")
        path = parsed.path
        if path != "/" and path.endswith("/"):
            path = path.rstrip("/")
        parsed    = parsed._replace(path=path)
        clean_url = urlunparse(parsed)
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

    # -- recursive worker ------------------------------------------------------

    def _dfs_crawl(url, depth):
        nonlocal page_count

        url = url.rstrip("/")

        if url in visited or url in reserved:
            return
        if depth > effective_max_depth:
            return
        if max_pages is not None and page_count >= max_pages:
            print(f"[max_pages={max_pages} reached — stopping]")
            return

        indent   = "  " * depth
        page_num = f"[{page_count + 1}" + (f"/{max_pages}]" if max_pages else "]")
        print(f"\n{indent}Visiting {page_num}: {url}")

        result = PageResult(url=url, depth=depth, status_code=0)

        try:
            # -- politeness delay ----------------------------------------------
            # Applied before every fetch except the very first page (depth 0,
            # page_count 0) so the initial request is instant.
            if politeness_delay > 0 and page_count > 0:
                time.sleep(politeness_delay)

            # -- fetch with retry ----------------------------------------------
            # Retries on transient errors (5xx, connection errors).
            # Does NOT retry on 404 — that's a permanent failure.
            MAX_RETRIES = 2
            RETRY_DELAY = 2   # seconds between retries

            response   = None
            last_error = None
            for attempt in range(MAX_RETRIES + 1):
                try:
                    response = requests.get(
                        url,
                        headers={"User-Agent": "Mozilla/5.0"},
                        timeout=5,
                    )
                    if response.status_code in (429, 500, 502, 503, 504):
                        if attempt < MAX_RETRIES:
                            print(f"{indent}  HTTP {response.status_code} — retrying ({attempt+1}/{MAX_RETRIES})...")
                            time.sleep(RETRY_DELAY)
                            continue
                    break   # success or non-retryable status
                except requests.exceptions.RequestException as e:
                    last_error = e
                    if attempt < MAX_RETRIES:
                        print(f"{indent}  Connection error — retrying ({attempt+1}/{MAX_RETRIES})...")
                        time.sleep(RETRY_DELAY)
                    else:
                        raise

            result.status_code = response.status_code

            canonical = response.url.rstrip("/")
            if canonical != url:
                print(f"{indent}Redirected to: {canonical}")
                if canonical in visited or canonical in reserved:
                    print(f"{indent}Skipped — canonical already visited/reserved")
                    return

            # -- HTTP status gate — checked BEFORE consuming a page slot ------
            if response.status_code != 200:
                result.error = f"HTTP {response.status_code}"
                print(f"{indent}Skipped — {result.error} (slot not consumed)")
                if tracker:
                    tracker.on_error(url, result.error)
                visited.add(url)
                visited.add(canonical)
                reserved.discard(url)
                reserved.discard(canonical)
                results.append(result)
                return

            # -- register as visited only after confirmed 200 -----------------
            visited.add(url)
            visited.add(canonical)
            reserved.discard(url)
            reserved.discard(canonical)
            page_count += 1
            result.url = canonical

            # -- content-type check (Stage 2) ---------------------------------
            # Done BEFORE tracker.on_visit so the tracker only sees pages
            # that will actually be parsed and returned.
            if not filter_chain.allow_response(url, response):
                ct = response.headers.get("Content-Type", "?")
                result.error = f"Content-Type blocked: {ct}"
                print(f"{indent}Skipped — {result.error}")
                if tracker:
                    tracker.on_error(canonical, result.error)
                results.append(result)
                return

            # -- notify tracker of confirmed-good visit -----------------------
            if tracker:
                tracker.on_visit(canonical, depth)

            # -- parse ---------------------------------------------------------
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

            # -- link collection -----------------------------------------------
            # collect branching_factor * 3 candidates as backup pool
            # so 404s don't leave empty branching slots
            pool_limit    = (branching_factor * 3) if branching_factor else None
            pool          = []
            seen_hrefs    = set()
            dropped_filt  = 0
            dropped_score = 0
            dropped_vis   = 0

            for tag in content.find_all("a", href=True):
                clean_url = normalize_and_filter_url(canonical, tag["href"])
                if not clean_url:
                    dropped_filt += 1
                    continue
                if clean_url in seen_hrefs:
                    continue
                if clean_url == canonical or clean_url in visited or clean_url in reserved:
                    dropped_vis += 1
                    continue
                if score_threshold is not None:
                    s = score_link(clean_url, tag.get_text())
                    if s < score_threshold:
                        if debug:
                            print(f"{indent}  [score={s:.2f}] dropped: {clean_url}")
                        dropped_score += 1
                        continue
                seen_hrefs.add(clean_url)
                pool.append(clean_url)
                if pool_limit and len(pool) >= pool_limit:
                    break

            # reserve the first branching_factor from pool before recursing
            first_batch = pool[:branching_factor] if branching_factor else pool
            for child in first_batch:
                reserved.add(child)

            print(f"{indent}  pool: {len(pool)} candidates | "
                  f"skipped: {dropped_vis} | "
                  f"score-filtered: {dropped_score} | "
                  f"url-filtered: {dropped_filt}")
            for child in pool[:branching_factor or len(pool)]:
                print(f"{indent}  -> {child}")

            # -- recurse -------------------------------------------------------
            # If a child 404s (not counted toward page_count), promote the
            # next backup candidate to fill the empty branching slot.
            real_children = 0
            pool_index    = 0

            while True:
                target = branching_factor if branching_factor else float("inf")
                if real_children >= target:
                    break
                if max_pages and page_count >= max_pages:
                    break
                if pool_index >= len(pool):
                    break

                child = pool[pool_index]
                pool_index += 1

                if child not in first_batch:
                    reserved.add(child)

                before = page_count
                reserved.discard(child)
                _dfs_crawl(child, depth + 1)

                if page_count > before:
                    real_children += 1
                else:
                    print(f"{indent}  backup promoted (child failed): trying next")

        except Exception as e:
            result.error = str(e)
            print(f"{indent}Error: {e}")
            if tracker:
                tracker.on_error(url, str(e))
            results.append(result)

    # -- kick off --------------------------------------------------------------
    _dfs_crawl(start_url, depth=0)
    successful = len([r for r in results if not r.error])
    print(f"\nDone — {page_count} visited, {successful} successful.")

    if tracker and tracker.auto_save:
        tracker.save()

    return results


# -- Usage examples ------------------------------------------------------------

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

#     # 4. with robots.txt + politeness delay
#     results = crawl(
#         start_url          = "https://www.tensorflow.org/tutorials",
#         output_formats     = ["markdown", "metadata"],
#         max_depth          = 2,
#         max_pages          = 10,
#         respect_robots_txt = True,
#         politeness_delay   = 1.0,
#     )

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
#         exclude_social_media = True,
#         respect_robots_txt   = True,
#         politeness_delay     = 0.5,
#         debug                = False,
#     )
