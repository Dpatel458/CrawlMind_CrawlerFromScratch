import time
import requests
from collections import deque
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse, urlunparse

from filters import (FilterChain, DomainFilter, URLPatternFilter,
                     ContentTypeFilter, SocialMediaFilter, RobotsTxtFilter)
from outputs import PageResult, OutputManager


def crawl(
    start_url,

    # -- strategy --------------------------------------------------------------
    strategy="dfs",             # "dfs" | "bfs"

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
                                # NOTE: only supported for strategy="dfs"

    # -- debug -----------------------------------------------------------------
    debug=False,
):
    """
    Unified entry point for DFS and BFS crawling.

    Parameters
    ----------
    start_url             : str
    strategy              : "dfs" | "bfs"  (default: "dfs")
    output_formats        : list[str | BaseOutput]  (None = crawl only)
    max_depth             : int    (None = unlimited)
    branching_factor      : int    (None = all links)
    max_pages             : int    (None = unlimited)
    score_threshold       : float  (None = off)
    word_count_threshold  : int    (None = off)
    url_pattern           : str    (None = off)
    exclude_external      : bool   (DomainFilter always active — kept for API compat)
    exclude_social_media  : bool
    extra_social_domains  : list[str]
    respect_robots_txt    : bool
    politeness_delay      : float  seconds between requests
    tracker               : CrawlTracker  (DFS only — ignored for BFS)
    debug                 : bool

    Returns
    -------
    list[PageResult]
    """

    # -- strategy validation ---------------------------------------------------
    if strategy not in ("dfs", "bfs"):
        raise ValueError(f"strategy must be 'dfs' or 'bfs', got {strategy!r}")

    if strategy == "bfs" and tracker is not None:
        print("[crawl] WARNING: tracker is not supported for BFS — ignoring.")
        tracker = None

    # -- output manager --------------------------------------------------------
    manager = OutputManager(output_formats or [], start_url=start_url)

    # -- filter chain ----------------------------------------------------------
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
    visited    = set()   # URLs confirmed fetched (200) — permanent
    reserved   = set()   # DFS only: URLs claimed by a parent before recursion.
                         # Always empty for BFS — discard() calls are no-ops.
    page_count = 0
    results    = []

    # -- depth guard -----------------------------------------------------------
    # Caps unbounded DFS recursion at 50 to avoid RecursionError.
    # BFS stores depth as a plain int in the queue tuple — no recursion risk —
    # but the same cap is applied for consistency.
    _MAX_SAFE_DEPTH  = 50
    effective_max_depth = max_depth if max_depth is not None else _MAX_SAFE_DEPTH

    # -- helpers ---------------------------------------------------------------

    def normalize_and_filter_url(current_url, href):
        base         = current_url if current_url.endswith("/") else current_url + "/"
        absolute_url = urljoin(base, href)
        parsed       = urlparse(absolute_url)
        parsed       = parsed._replace(fragment="", query="")
        path         = parsed.path
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

    # -- shared fetch + parse helper -------------------------------------------
    #
    # Used by both _dfs_crawl and _bfs_crawl.
    # Handles: politeness delay, fetch with retry, redirect detection,
    #          HTTP status gate, content-type gate, HTML parsing + noise
    #          stripping, word count check, output extraction.
    #
    # Returns
    # -------
    # (True,  canonical, content)  — page fetched, parsed, appended to results
    # (False, None,      None)     — skipped for any reason (non-200, CT blocked,
    #                                word count, exception); result already appended
    #
    def _fetch_page(url, depth):
        nonlocal page_count

        indent   = "  " * depth
        page_num = f"[{page_count + 1}" + (f"/{max_pages}]" if max_pages else "]")
        print(f"\n{indent}Visiting {page_num}: {url}")

        result = PageResult(url=url, depth=depth, status_code=0)

        try:
            # -- politeness delay ----------------------------------------------
            if politeness_delay > 0 and page_count > 0:
                time.sleep(politeness_delay)

            # -- fetch with retry ----------------------------------------------
            MAX_RETRIES = 2
            RETRY_DELAY = 2

            response = None
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
                    break
                except requests.exceptions.RequestException as e:
                    if attempt < MAX_RETRIES:
                        print(f"{indent}  Connection error — retrying ({attempt+1}/{MAX_RETRIES})...")
                        time.sleep(RETRY_DELAY)
                    else:
                        raise

            result.status_code = response.status_code

            # -- redirect detection --------------------------------------------
            canonical = response.url.rstrip("/")
            if canonical != url:
                print(f"{indent}Redirected to: {canonical}")
                # reserved is always empty for BFS — check is a no-op
                if canonical in visited or canonical in reserved:
                    print(f"{indent}Skipped — canonical already visited/reserved")
                    return False, None, None

            # -- HTTP status gate ----------------------------------------------
            # Non-200s: mark visited so we never retry, append error result,
            # return False so caller skips link collection.
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
                return False, None, None

            # -- register as visited -------------------------------------------
            visited.add(url)
            visited.add(canonical)
            reserved.discard(url)
            reserved.discard(canonical)
            page_count += 1
            result.url = canonical

            # -- content-type check (Stage 2) ----------------------------------
            # Done before tracker.on_visit so tracker only sees parseable pages.
            if not filter_chain.allow_response(url, response):
                ct = response.headers.get("Content-Type", "?")
                result.error = f"Content-Type blocked: {ct}"
                print(f"{indent}Skipped — {result.error}")
                if tracker:
                    tracker.on_error(canonical, result.error)
                results.append(result)
                return False, None, None

            # -- tracker: confirmed good visit (DFS only; tracker=None for BFS)
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

            # -- word count gate -----------------------------------------------
            text = " ".join(content.get_text(" ", strip=True).split())
            if word_count_threshold is not None:
                wc = len(text.split())
                if wc < word_count_threshold:
                    result.error = f"word_count={wc} < threshold={word_count_threshold}"
                    print(f"{indent}Skipped — {result.error}")
                    if tracker:
                        tracker.on_error(canonical, result.error)
                    results.append(result)
                    return False, None, None

            print(f"{indent}({len(text.split())} words): {text[:200]}...")

            if tracker:
                tracker.on_success(canonical, text)

            manager.extract_all(result, content, raw_soup, response)
            results.append(result)

            return True, canonical, content

        except Exception as e:
            result.error = str(e)
            print(f"{indent}Error: {e}")
            if tracker:
                tracker.on_error(url, str(e))
            results.append(result)
            return False, None, None

    # -- DFS: recursive worker -------------------------------------------------

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

        success, canonical, content = _fetch_page(url, depth)
        if not success:
            return

        indent = "  " * depth

        # -- link collection ---------------------------------------------------
        # Collect branching_factor * 3 candidates as a backup pool so that
        # 404s don't leave empty branching slots.
        pool_limit    = (branching_factor * 3) if branching_factor else None
        pool          = []
        seen_hrefs    = set()
        dropped_filt  = 0
        dropped_score = 0
        dropped_vis   = 0

        for tag in content.find_all("a", href=True):
            child = normalize_and_filter_url(canonical, tag["href"])
            if not child:
                dropped_filt += 1
                continue
            if child in seen_hrefs:
                continue
            if child == canonical or child in visited or child in reserved:
                dropped_vis += 1
                continue
            if score_threshold is not None:
                s = score_link(child, tag.get_text())
                if s < score_threshold:
                    if debug:
                        print(f"{indent}  [score={s:.2f}] dropped: {child}")
                    dropped_score += 1
                    continue
            seen_hrefs.add(child)
            pool.append(child)
            if pool_limit and len(pool) >= pool_limit:
                break

        # Reserve the first branching_factor children before recursing so
        # sibling subtrees cannot claim the same URLs.
        first_batch = pool[:branching_factor] if branching_factor else pool
        for child in first_batch:
            reserved.add(child)

        print(f"{indent}  pool: {len(pool)} candidates | "
              f"skipped: {dropped_vis} | "
              f"score-filtered: {dropped_score} | "
              f"url-filtered: {dropped_filt}")
        for child in pool[:branching_factor or len(pool)]:
            print(f"{indent}  -> {child}")

        # -- recurse -----------------------------------------------------------
        # If a child 404s it doesn't count toward page_count, so promote the
        # next backup from the pool to fill the empty branching slot.
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

    # -- BFS: iterative worker -------------------------------------------------

    def _bfs_crawl():
        nonlocal page_count

        start = start_url.rstrip("/")

        # seen = visited ∪ enqueued.
        # Checked before every queue.append() to prevent the same URL from
        # entering the queue twice (e.g. two parent pages both linking to /about).
        queue = deque([(start, 0)])
        seen  = {start}

        while queue:
            url, depth = queue.popleft()

            if max_pages is not None and page_count >= max_pages:
                print(f"[max_pages={max_pages} reached — stopping]")
                break
            if depth > effective_max_depth:
                continue
            if url in visited:
                # Reached via redirect from another URL that was already fetched.
                continue

            success, canonical, content = _fetch_page(url, depth)
            if not success:
                continue

            indent = "  " * depth

            # -- enqueue children ----------------------------------------------
            children_queued = 0
            dropped_filt    = 0
            dropped_score   = 0
            dropped_vis     = 0

            for tag in content.find_all("a", href=True):
                # Stop collecting once we have branching_factor children queued.
                # Unlike DFS there is no backup pool needed — if a child 404s
                # the queue simply moves on to the next item naturally.
                if branching_factor and children_queued >= branching_factor:
                    break

                child = normalize_and_filter_url(canonical, tag["href"])
                if not child:
                    dropped_filt += 1
                    continue
                if child in seen:
                    dropped_vis += 1
                    continue
                if score_threshold is not None:
                    s = score_link(child, tag.get_text())
                    if s < score_threshold:
                        if debug:
                            print(f"{indent}  [score={s:.2f}] dropped: {child}")
                        dropped_score += 1
                        continue

                seen.add(child)
                queue.append((child, depth + 1))
                children_queued += 1

            print(f"{indent}  queued: {children_queued} | "
                  f"skipped: {dropped_vis} | "
                  f"score-filtered: {dropped_score} | "
                  f"url-filtered: {dropped_filt} | "
                  f"queue size: {len(queue)}")

    # -- kick off --------------------------------------------------------------
    if strategy == "dfs":
        _dfs_crawl(start_url, depth=0)
    else:
        _bfs_crawl()

    successful = len([r for r in results if not r.error])
    print(f"\nDone — {page_count} visited, {successful} successful.")

    if tracker and tracker.auto_save:
        tracker.save()

    return results


# -- Usage examples ------------------------------------------------------------

# if __name__ == "__main__":

#     # BFS — breadth-first (visits shallowest pages first)
#     results = crawl(
#         start_url        = "https://www.tensorflow.org/tutorials",
#         strategy         = "bfs",
#         output_formats   = ["markdown", "metadata"],
#         max_depth        = 2,
#         branching_factor = 3,
#         max_pages        = 15,
#         politeness_delay = 0.5,
#     )

#     # DFS — depth-first (default, unchanged behaviour)
#     results = crawl(
#         start_url        = "https://www.tensorflow.org/tutorials",
#         strategy         = "dfs",
#         output_formats   = ["markdown", "metadata", "links"],
#         max_depth        = 3,
#         branching_factor = 3,
#         max_pages        = 15,
#         politeness_delay = 0.5,
#     )

#     # Fully controlled BFS
#     results = crawl(
#         start_url            = "https://www.tensorflow.org/tutorials",
#         strategy             = "bfs",
#         output_formats       = ["cleaned_html", "markdown", "metadata", "links"],
#         max_depth            = 3,
#         branching_factor     = 4,
#         max_pages            = 20,
#         score_threshold      = 0.1,
#         word_count_threshold = 10,
#         exclude_social_media = True,
#         respect_robots_txt   = True,
#         politeness_delay     = 0.5,
#         debug                = False,
#     )
