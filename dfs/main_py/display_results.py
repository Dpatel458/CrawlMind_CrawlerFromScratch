from crawler import crawl
from tracker import CrawlTracker
from printer import print_results, print_metadata, print_links, print_markdown, print_cleaned_html
from saver  import save_results

# ── toggles ───────────────────────────────────────────────────────────────────
SAVE_TREE  = True   # save crawl_tree.json
SAVE_FILES = True   # save results to disk (crawl_output/)

# ── tracker ───────────────────────────────────────────────────────────────────
tracker = CrawlTracker(output_path="crawl_tree.json", auto_save=True) if SAVE_TREE else None

# ── crawl ─────────────────────────────────────────────────────────────────────
results = crawl(
    start_url            = "https://docs.python.org",
    output_formats       = ["cleaned_html", "markdown", "metadata", "links"],
    max_depth            = 5,
    branching_factor     = 5,
    max_pages            = 50,
    score_threshold      = 0.1,
    word_count_threshold = 30,
    url_pattern          = "https://docs.python.org",
    exclude_external     = True,
    exclude_social_media = True,
    debug                = False,
    tracker              = tracker,
)

# ── save to files ─────────────────────────────────────────────────────────────
if SAVE_FILES:
    save_results(
        results,
        output_dir = "D:/Adrta/CRAWLMIND/CrawlMind_CrawlerFromScratch/dfs/crawl_output",
        formats  = ["markdown"],  # override if needed
        # run_label= "tf_tutorials",            # custom folder name
    )

# ── display ───────────────────────────────────────────────────────────────────
print_results(results, formats=["cleaned_html"])

# print_results(results)                          # all formats
# print_results(results, formats=["metadata", "links"])
# print_metadata(results)
# print_links(results, max_show=10)
# print_markdown(results, preview=2000)
# print_cleaned_html(results, preview=1000)
# print_results(results[0], formats=["markdown"]) # single page