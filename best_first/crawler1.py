import re
import time
import requests
from collections import deque
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse, urlunparse

from filters import (FilterChain, DomainFilter, URLPatternFilter,
                     ContentTypeFilter, SocialMediaFilter, RobotsTxtFilter)
from outputs import PageResult, OutputManager


# =============================================================================
# KeywordRelevanceScorer
# =============================================================================
#
# Mirrors crawl4ai's KeywordRelevanceScorer API and extends it with
# full-content scoring for two-phase best-first crawling.
#
# Two scoring methods:
#
#   pre_score(url, anchor_text)      — BEFORE visiting (URL path + anchor)
#   content_score(page_text)         — AFTER visiting  (full page text)
#   score(url, anchor_text)          — alias for pre_score (crawl4ai compat)
#
# --- pre_score formula (mirrors crawl4ai) ---
#   url_ratio    = matched_kw_in_url_path / total_kw
#   anchor_ratio = matched_kw_in_anchor   / total_kw
#   raw          = url_ratio * 0.7 + anchor_ratio * 0.3
#   score        = clamp(raw * weight, 0.0, 1.0)
#
# --- content_score formula ---
#   density  = distinct_kw_hits / total_kw
#   tf_score = sum(min(freq/FREQ_CAP, 1.0) / total_kw)  per keyword
#              — rewards multiple mentions but caps keyword-stuffing
#   raw      = density * 0.5 + tf_score * 0.5
#   score    = clamp(raw * weight, 0.0, 1.0)
#
# --- two-phase blend in _best_first_crawl ---
#   final_child_score = content_blend * parent_content_score
#                     + (1 - content_blend) * child_pre_score
#
#   content_blend=0.6 (default): parent page content has 60% influence,
#   child URL/anchor has 40%. Children of relevant pages rank higher even
#   when their own URLs look generic (/page-2, /part-3, etc.).
#
class KeywordRelevanceScorer:
    """
    Score URLs and page content against a list of keywords.

    Parameters
    ----------
    keywords : list[str]   — plain strings, all weighted equally
    weight   : float       — overall multiplier in [0.0, 1.0] (crawl4ai API)
    """

    URL_WEIGHT     = 0.7
    ANCHOR_WEIGHT  = 0.3
    DENSITY_WEIGHT = 0.5
    TF_WEIGHT      = 0.5
    FREQ_CAP       = 10      # keyword hits beyond this give no extra credit

    def __init__(self, keywords, weight=1.0):
        if not keywords:
            raise ValueError("KeywordRelevanceScorer requires at least one keyword.")
        if not (0.0 <= weight <= 1.0):
            raise ValueError("weight must be in [0.0, 1.0].")
        self.weight    = weight
        self._keywords = [kw.lower().strip() for kw in keywords]

    def pre_score(self, url, anchor_text=""):
        """Score a candidate link BEFORE visiting — URL path + anchor text only."""
        url_lower    = urlparse(url).path.lower()
        anchor_lower = (anchor_text or "").lower()
        total_kw     = len(self._keywords)

        url_hits    = sum(1 for kw in self._keywords if kw in url_lower)
        anchor_hits = sum(1 for kw in self._keywords if kw in anchor_lower)

        raw = (url_hits / total_kw) * self.URL_WEIGHT + \
              (anchor_hits / total_kw) * self.ANCHOR_WEIGHT
        return round(min(raw * self.weight, 1.0), 4)

    def content_score(self, page_text):
        """Score a page AFTER visiting — full cleaned text."""
        if not page_text:
            return 0.0

        text_lower = page_text.lower()
        total_kw   = len(self._keywords)
        norm_w     = 1.0 / total_kw

        # keyword density: how many distinct keywords appear at all
        hit_count = sum(1 for kw in self._keywords if kw in text_lower)
        density   = hit_count / total_kw

        # term frequency: reward repeated mentions up to FREQ_CAP
        tf_score = sum(
            min(len(re.findall(re.escape(kw), text_lower)) / self.FREQ_CAP, 1.0) * norm_w
            for kw in self._keywords
        )

        raw = density * self.DENSITY_WEIGHT + tf_score * self.TF_WEIGHT
        return round(min(raw * self.weight, 1.0), 4)

    def score(self, url, anchor_text=""):
        """Backward-compatible alias for pre_score (crawl4ai API)."""
        return self.pre_score(url, anchor_text)


# =============================================================================
# crawl()
# =============================================================================

def crawl(
    start_url,

    # -- strategy --------------------------------------------------------------
    strategy="dfs",             # "dfs" | "bfs" | "best_first"

    # -- output ----------------------------------------------------------------
    output_formats=None,        # list[str | BaseOutput] | None

    # -- crawl controls --------------------------------------------------------
    max_depth=None,
    branching_factor=None,
    max_pages=None,

    # -- best-first controls ---------------------------------------------------
    best_first_keywords=None,   # KeywordRelevanceScorer | list[str] | None

    # -- content blend (best_first only) ---------------------------------------
    # Controls how much the parent page's full-text relevance influences
    # child link priorities.
    #   0.0 = URL+anchor only  (crawl4ai baseline — no content scoring)
    #   1.0 = content only     (ignore URL/anchor entirely)
    #   0.6 = default          (60% content, 40% URL/anchor)
    content_blend=0.6,

    # -- content controls ------------------------------------------------------
    score_threshold=None,       # DFS/BFS only
    word_count_threshold=None,

    # -- filter toggles --------------------------------------------------------
    url_pattern=None,
    exclude_external=False,
    exclude_social_media=False,
    extra_social_domains=None,
    respect_robots_txt=False,

    # -- politeness ------------------------------------------------------------
    politeness_delay=0.0,

    # -- tracker ---------------------------------------------------------------
    tracker=None,               # DFS only

    # -- debug -----------------------------------------------------------------
    debug=False,
):
    """
    Unified entry point for DFS, BFS, and Best-First crawling.

    Best-First uses two-phase scoring
    ----------------------------------
    Phase 1 — pre_score(url, anchor_text)
      Called when a child link is discovered (before fetching).
      Uses URL path + anchor text → sets initial heap priority.

    Phase 2 — content_score(page_text)
      Called after visiting a page (full cleaned text available).
      Rescores children's priorities via content_blend:

        final_child_score = content_blend       * parent_content_score
                          + (1 - content_blend) * child_pre_score

    Result metadata (best_first only)
    -----------------------------------
    Each PageResult.metadata contains:
      "pre_score"     : float  — URL+anchor score at discovery
      "content_score" : float  — full-text score after visiting
      "final_score"   : float  — blended score used for heap priority

    Parameters
    ----------
    start_url             : str
    strategy              : "dfs" | "bfs" | "best_first"
    output_formats        : list[str | BaseOutput] | None
    max_depth             : int | None
    branching_factor      : int | None
    max_pages             : int | None
    best_first_keywords   : KeywordRelevanceScorer | list[str]
    content_blend         : float in [0.0, 1.0]  (default 0.6)
    score_threshold       : float | None  (DFS/BFS only)
    word_count_threshold  : int | None
    url_pattern           : str | None
    exclude_external      : bool
    exclude_social_media  : bool
    extra_social_domains  : list[str] | None
    respect_robots_txt    : bool
    politeness_delay      : float
    tracker               : CrawlTracker | None  (DFS only)
    debug                 : bool

    Returns
    -------
    list[PageResult]
    """

    # -- validation ------------------------------------------------------------
    if strategy not in ("dfs", "bfs", "best_first"):
        raise ValueError(f"strategy must be 'dfs', 'bfs', or 'best_first', got {strategy!r}")

    if strategy == "best_first" and best_first_keywords is None:
        raise ValueError(
            "strategy='best_first' requires best_first_keywords.\n"
            'Pass a list of strings: best_first_keywords=["tensorflow", "tutorial"]\n'
            'or a scorer:            best_first_keywords=KeywordRelevanceScorer([...], weight=0.8)'
        )

    if not (0.0 <= content_blend <= 1.0):
        raise ValueError("content_blend must be in [0.0, 1.0].")

    # -- normalise scorer ------------------------------------------------------
    if strategy == "best_first":
        if isinstance(best_first_keywords, (list, tuple)):
            scorer = KeywordRelevanceScorer(keywords=list(best_first_keywords), weight=1.0)
        elif isinstance(best_first_keywords, KeywordRelevanceScorer):
            scorer = best_first_keywords
        else:
            raise TypeError(
                "best_first_keywords must be a KeywordRelevanceScorer "
                "or a plain list of keyword strings."
            )
    else:
        scorer = None

    # -- warnings --------------------------------------------------------------
    if strategy in ("bfs", "best_first") and tracker is not None:
        print(f"[crawl] WARNING: tracker not supported for strategy='{strategy}' — ignoring.")
        tracker = None

    if strategy == "best_first" and score_threshold is not None:
        print("[crawl] NOTE: score_threshold is ignored for best_first — "
              "all URLs are enqueued and visited in score order.")

    if strategy != "best_first" and content_blend != 0.6:
        print("[crawl] NOTE: content_blend has no effect for DFS/BFS strategies.")

    # -- output manager + filter chain -----------------------------------------
    manager = OutputManager(output_formats or [], start_url=start_url)

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
    visited    = set()
    reserved   = set()   # DFS only
    page_count = 0
    results    = []

    _MAX_SAFE_DEPTH     = 50
    effective_max_depth = max_depth if max_depth is not None else _MAX_SAFE_DEPTH

    # -------------------------------------------------------------------------
    # helpers
    # -------------------------------------------------------------------------

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
        """Legacy path-overlap scorer for DFS/BFS score_threshold."""
        base_kw    = set(urlparse(start_url).path.strip("/").split("/")) - {""}
        link_parts = set(urlparse(href).path.strip("/").split("/"))      - {""}
        overlap    = len(base_kw & link_parts)
        score      = min(overlap / max(len(base_kw), 1), 1.0) * 0.5
        score     += 0.2 - min(len(link_parts) * 0.05, 0.2)
        if len(anchor_text.strip()) > 3:
            score += 0.3
        return round(min(score, 1.0), 3)

    # -------------------------------------------------------------------------
    # _fetch_page  —  shared by all three strategies
    # Returns: (True, canonical, content_soup, page_text) | (False, None, None, None)
    # page_text is returned separately so best_first can run content_score()
    # without re-parsing.
    # -------------------------------------------------------------------------
    def _fetch_page(url, depth):
        nonlocal page_count

        indent   = "  " * depth
        page_num = f"[{page_count + 1}" + (f"/{max_pages}]" if max_pages else "]")
        print(f"\n{indent}Visiting {page_num}: {url}")

        result = PageResult(url=url, depth=depth, status_code=0)

        try:
            if politeness_delay > 0 and page_count > 0:
                time.sleep(politeness_delay)

            MAX_RETRIES = 2
            RETRY_DELAY = 2
            response    = None

            for attempt in range(MAX_RETRIES + 1):
                try:
                    response = requests.get(
                        url, headers={"User-Agent": "Mozilla/5.0"}, timeout=5,
                    )
                    if response.status_code in (429, 500, 502, 503, 504):
                        if attempt < MAX_RETRIES:
                            print(f"{indent}  HTTP {response.status_code} — retrying ({attempt+1}/{MAX_RETRIES})...")
                            time.sleep(RETRY_DELAY)
                            continue
                    break
                except requests.exceptions.RequestException:
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
                    return False, None, None, None

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
                return False, None, None, None

            visited.add(url)
            visited.add(canonical)
            reserved.discard(url)
            reserved.discard(canonical)
            page_count += 1
            result.url = canonical

            if not filter_chain.allow_response(url, response):
                ct = response.headers.get("Content-Type", "?")
                result.error = f"Content-Type blocked: {ct}"
                print(f"{indent}Skipped — {result.error}")
                if tracker:
                    tracker.on_error(canonical, result.error)
                results.append(result)
                return False, None, None, None

            if tracker:
                tracker.on_visit(canonical, depth)

            raw_soup = BeautifulSoup(response.text, "html.parser")
            soup     = BeautifulSoup(response.text, "html.parser")

            for tag in soup(["script", "style", "nav", "header", "footer",
                              "aside", "form", "iframe", "noscript"]):
                tag.decompose()

            NOISE_PATTERNS = [
                "header", "footer", "nav", "cookie", "banner", "breadcrumb",
                "sidebar", "toc", "toolbar", "menu", "announcement",
                "notification", "skip", "search", "devsite-band",
                "devsite-collection", "devsite-rating", "devsite-thumb",
                "devsite-page-rating", "devsite-bookmark",
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

            page_text = " ".join(content.get_text(" ", strip=True).split())

            if word_count_threshold is not None:
                wc = len(page_text.split())
                if wc < word_count_threshold:
                    result.error = f"word_count={wc} < threshold={word_count_threshold}"
                    print(f"{indent}Skipped — {result.error}")
                    if tracker:
                        tracker.on_error(canonical, result.error)
                    results.append(result)
                    return False, None, None, None

            print(f"{indent}({len(page_text.split())} words): {page_text[:200]}...")

            if tracker:
                tracker.on_success(canonical, page_text)

            manager.extract_all(result, content, raw_soup, response)
            results.append(result)
            return True, canonical, content, page_text

        except Exception as e:
            result.error = str(e)
            print(f"{indent}Error: {e}")
            if tracker:
                tracker.on_error(url, str(e))
            results.append(result)
            return False, None, None, None

    # =========================================================================
    # DFS — recursive
    # =========================================================================

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

        success, canonical, content, _ = _fetch_page(url, depth)
        if not success:
            return

        indent     = "  " * depth
        pool_limit = (branching_factor * 3) if branching_factor else None
        pool       = []
        seen_hrefs = set()
        dropped_filt = dropped_score = dropped_vis = 0

        for tag in content.find_all("a", href=True):
            child = normalize_and_filter_url(canonical, tag["href"])
            if not child:
                dropped_filt += 1; continue
            if child in seen_hrefs:
                continue
            if child == canonical or child in visited or child in reserved:
                dropped_vis += 1; continue
            if score_threshold is not None:
                s = score_link(child, tag.get_text())
                if s < score_threshold:
                    if debug:
                        print(f"{indent}  [score={s:.2f}] dropped: {child}")
                    dropped_score += 1; continue
            seen_hrefs.add(child)
            pool.append(child)
            if pool_limit and len(pool) >= pool_limit:
                break

        first_batch = pool[:branching_factor] if branching_factor else pool
        for child in first_batch:
            reserved.add(child)

        print(f"{indent}  pool: {len(pool)} | skipped: {dropped_vis} | "
              f"score-filtered: {dropped_score} | url-filtered: {dropped_filt}")
        for child in pool[:branching_factor or len(pool)]:
            print(f"{indent}  -> {child}")

        real_children = 0
        pool_index    = 0

        while True:
            target = branching_factor if branching_factor else float("inf")
            if real_children >= target or (max_pages and page_count >= max_pages):
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

    # =========================================================================
    # BFS — iterative with deque
    # =========================================================================

    def _bfs_crawl():
        nonlocal page_count

        start = start_url.rstrip("/")
        queue = deque([(start, 0)])
        seen  = {start}

        while queue:
            url, depth = queue.popleft()

            if max_pages is not None and page_count >= max_pages:
                print(f"[max_pages={max_pages} reached — stopping]")
                break
            if depth > effective_max_depth or url in visited:
                continue

            success, canonical, content, _ = _fetch_page(url, depth)
            if not success:
                continue

            indent = "  " * depth
            children_queued = dropped_filt = dropped_score = dropped_vis = 0

            for tag in content.find_all("a", href=True):
                if branching_factor and children_queued >= branching_factor:
                    break
                child = normalize_and_filter_url(canonical, tag["href"])
                if not child:
                    dropped_filt += 1; continue
                if child in seen:
                    dropped_vis += 1; continue
                if score_threshold is not None:
                    s = score_link(child, tag.get_text())
                    if s < score_threshold:
                        if debug:
                            print(f"{indent}  [score={s:.2f}] dropped: {child}")
                        dropped_score += 1; continue
                seen.add(child)
                queue.append((child, depth + 1))
                children_queued += 1

            print(f"{indent}  queued: {children_queued} | skipped: {dropped_vis} | "
                  f"score-filtered: {dropped_score} | url-filtered: {dropped_filt} | "
                  f"queue size: {len(queue)}")

    # =========================================================================
    # Best-First — two-phase content-aware priority crawl
    # =========================================================================
    #
    # Heap item: (-final_score, depth, url, anchor_text, pre_score)
    #
    # Phase 1 (pre_score) happens at link-discovery time — cheap, no fetch.
    # Phase 2 (content_score) happens right after each page is fetched and
    #   parsed. It scores the parent's full text, then rescores all children:
    #
    #     final = content_blend * parent_content_score
    #           + (1 - content_blend) * child_pre_score
    #
    def _best_first_crawl():
        nonlocal page_count
        import heapq

        start = start_url.rstrip("/")
        seen  = {start}

        # Start URL seeded at score=1.0 — always visited first.
        # Item: (-final_score, depth, url, anchor_text, pre_score)
        heap = [(-1.0, 0, start, "", 1.0)]
        heapq.heapify(heap)

        while heap:
            neg_score, depth, url, anchor_text, pre_score = heapq.heappop(heap)
            final_score = -neg_score

            if max_pages is not None and page_count >= max_pages:
                print(f"[max_pages={max_pages} reached — stopping]")
                break
            if depth > effective_max_depth:
                continue   # keep draining — shallower items may still exist
            if url in visited:
                continue

            indent = "  " * depth
            print(f"  [final={final_score:.4f} | pre={pre_score:.4f}]", end=" ")

            success, canonical, content_soup, page_text = _fetch_page(url, depth)
            if not success:
                continue

            # -- Phase 2: score this page's full content -----------------------
            this_content_score = scorer.content_score(page_text) if page_text else 0.0

            # -- store all three scores on the result --------------------------
            if results and results[-1].url == canonical:
                if results[-1].metadata is None:
                    results[-1].metadata = {}
                results[-1].metadata["pre_score"]     = round(pre_score, 4)
                results[-1].metadata["content_score"] = round(this_content_score, 4)
                results[-1].metadata["final_score"]   = round(final_score, 4)

            if debug:
                print(f"\n{indent}  content_score={this_content_score:.4f} for {canonical}")

            # -- collect valid child links -------------------------------------
            candidates   = []
            dropped_filt = dropped_vis = 0

            for tag in content_soup.find_all("a", href=True):
                child      = normalize_and_filter_url(canonical, tag["href"])
                child_text = tag.get_text(strip=True)
                if not child:
                    dropped_filt += 1; continue
                if child in seen:
                    dropped_vis += 1; continue
                candidates.append((child, child_text))

            # -- Phase 1 + blend: score each child then blend with parent ------
            scored = []
            for child_url, child_text in candidates:
                child_pre   = scorer.pre_score(child_url, child_text)
                child_final = round(min(
                    content_blend       * this_content_score +
                    (1 - content_blend) * child_pre,
                    1.0
                ), 4)
                scored.append((child_final, child_pre, child_url, child_text))

            # Sort descending — best children pushed first
            scored.sort(key=lambda x: x[0], reverse=True)

            # Apply branching_factor AFTER scoring — drops lowest scorers only
            if branching_factor:
                scored = scored[:branching_factor]

            children_added = 0
            for child_final, child_pre, child_url, child_text in scored:
                if child_url in seen:   # safety net for normalisation collisions
                    continue
                seen.add(child_url)
                heapq.heappush(heap, (-child_final, depth + 1, child_url, child_text, child_pre))
                children_added += 1
                if debug:
                    print(f"{indent}  [final={child_final:.4f} pre={child_pre:.4f}] -> {child_url}")

            print(f"{indent}  content_score={this_content_score:.4f} | "
                  f"children: {len(scored)} | enqueued: {children_added} | "
                  f"skipped: {dropped_vis} | url-filtered: {dropped_filt} | "
                  f"heap: {len(heap)}")

    # =========================================================================
    # kick off
    # =========================================================================

    if strategy == "dfs":
        _dfs_crawl(start_url, depth=0)
    elif strategy == "bfs":
        _bfs_crawl()
    else:
        _best_first_crawl()

    successful = len([r for r in results if not r.error])
    print(f"\nDone — {page_count} visited, {successful} successful.")

    if tracker and tracker.auto_save:
        tracker.save()

    return results


# =============================================================================
# Usage examples
# =============================================================================

# if __name__ == "__main__":

#     # --- DFS ---
#     results = crawl(
#         start_url        = "https://www.tensorflow.org/tutorials",
#         strategy         = "dfs",
#         output_formats   = ["markdown", "metadata", "links"],
#         max_depth        = 3,
#         branching_factor = 3,
#         max_pages        = 15,
#         politeness_delay = 0.5,
#     )

#     # --- BFS ---
#     results = crawl(
#         start_url        = "https://www.tensorflow.org/tutorials",
#         strategy         = "bfs",
#         output_formats   = ["markdown", "metadata"],
#         max_depth        = 2,
#         branching_factor = 3,
#         max_pages        = 15,
#         politeness_delay = 0.5,
#     )

#     # --- Best-First: plain keyword list (simplest usage) ---
#     results = crawl(
#         start_url            = "https://www.tensorflow.org/tutorials",
#         strategy             = "best_first",
#         best_first_keywords  = ["tensorflow", "tutorial", "keras", "beginner"],
#         output_formats       = ["markdown", "metadata"],
#         max_depth            = 2,
#         branching_factor     = 5,
#         max_pages            = 20,
#         word_count_threshold = 10,
#         exclude_social_media = True,
#         respect_robots_txt   = True,
#         politeness_delay     = 0.5,
#         debug                = False,
#     )

#     # --- Best-First: explicit scorer + custom blend ---
#     scorer = KeywordRelevanceScorer(
#         keywords = ["neural network", "tutorial", "keras", "beginner"],
#         weight   = 0.7,
#     )
#     results = crawl(
#         start_url           = "https://www.tensorflow.org/tutorials",
#         strategy            = "best_first",
#         best_first_keywords = scorer,
#         content_blend       = 0.7,   # 70% content, 30% URL/anchor
#         output_formats      = ["markdown", "metadata"],
#         max_depth           = 3,
#         branching_factor    = 5,
#         max_pages           = 20,
#         politeness_delay    = 0.5,
#         debug               = True,  # shows per-URL scores
#     )

#     # --- Inspect scores ---
#     for r in results:
#         m = r.metadata or {}
#         print(
#             f"final={m.get('final_score','n/a'):.4f} | "
#             f"pre={m.get('pre_score','n/a'):.4f} | "
#             f"content={m.get('content_score','n/a'):.4f} | "
#             f"depth={r.depth} | {r.url}"
#         )

#     # --- Best-First: URL/anchor only (crawl4ai baseline, content_blend=0.0) ---
#     results = crawl(
#         start_url           = "https://www.tensorflow.org/tutorials",
#         strategy            = "best_first",
#         best_first_keywords = ["tensorflow", "tutorial"],
#         content_blend       = 0.0,   # disables content scoring entirely
#         ...
#     )