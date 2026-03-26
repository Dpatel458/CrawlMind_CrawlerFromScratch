import os
from crawler1 import crawl, KeywordRelevanceScorer
from tracker import CrawlTracker
from printer import print_results, print_metadata, print_links, print_markdown, print_cleaned_html
from saver  import save_results

# -- toggles ------------------------------------------------------------------
SAVE_TREE  = False
SAVE_FILES = True

# -- output directory ---------------------------------------------------------
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "crawl_output")

# -- tracker ------------------------------------------------------------------
tracker = CrawlTracker(output_path="crawl_tree.json", auto_save=True) if SAVE_TREE else None

# -- scorer -------------------------------------------------------------------
# KeywordRelevanceScorer API mirrors crawl4ai exactly:
#   keywords : plain list[str]  — all weighted equally
#   weight   : float [0.0, 1.0] — overall multiplier (crawl4ai's weight param)
#
# Two-phase scoring:
#   pre_score(url, anchor)   — at link discovery (URL + anchor text)
#   content_score(page_text) — after page fetch  (full cleaned text)
#
scorer = KeywordRelevanceScorer(
    keywords=["tensorflow", "tutorial", "guide", "introduction", "keras", "beginner"],
    weight=0.7,
)

# -- crawl --------------------------------------------------------------------
results = crawl(
    strategy             = "best_first",
    best_first_keywords  = scorer,

    # content_blend: how much parent page's full-text score influences
    # child priorities.  0.0 = URL/anchor only  |  1.0 = content only
    # 0.6 default: 60% content, 40% URL+anchor
    content_blend        = 0.6,

    start_url            = "https://www.tensorflow.org/tutorials",
    output_formats       = ["cleaned_html", "markdown", "metadata", "links"],
    max_depth            = 2,
    branching_factor     = 3,
    max_pages            = 10,
    word_count_threshold = 10,
    exclude_social_media = True,
    respect_robots_txt   = True,
    politeness_delay     = 0.5,
    debug                = False,
    tracker              = tracker,
)

# -- save to files ------------------------------------------------------------
if SAVE_FILES:
    save_results(results, output_dir=OUTPUT_DIR, formats=["markdown"])

# -- display ------------------------------------------------------------------
print_results(results, formats=["markdown"])

# -- score summary ------------------------------------------------------------
print("\n--- Score summary ---")
print(f"  {'URL':<55} {'final':>7} {'pre':>7} {'content':>9} {'depth':>6}")
print(f"  {'-'*55} {'-'*7} {'-'*7} {'-'*9} {'-'*6}")
for r in results:
    m     = r.metadata or {}
    final = m.get("final_score",   "n/a")
    pre   = m.get("pre_score",     "n/a")
    cont  = m.get("content_score", "n/a")
    url   = r.url or r.url
    short = (url[:52] + "...") if len(url) > 55 else url
    print(f"  {short:<55} {str(final):>7} {str(pre):>7} {str(cont):>9} {r.depth:>6}")