import os
from crawler1 import crawl
from tracker import CrawlTracker
from printer import print_results, print_metadata, print_links, print_markdown, print_cleaned_html
from saver  import save_results

# -- toggles ------------------------------------------------------------------
SAVE_TREE  = True   # save crawl_tree.json
SAVE_FILES = True   # save results to disk

# -- output directory ---------------------------------------------------------
# Use an absolute path or leave as a relative path.
# Relative paths resolve from wherever you launch Python.
# os.path.dirname(__file__) anchors it next to this script file.
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "crawl_output")

# -- tracker ------------------------------------------------------------------
tracker = CrawlTracker(output_path="crawl_tree.json", auto_save=True) if SAVE_TREE else None

# -- crawl --------------------------------------------------------------------
results = crawl(
    strategy="bfs",             # "dfs" | "bfs"
    start_url            = "https://www.tensorflow.org/tutorials",
    output_formats       = ["cleaned_html", "markdown", "metadata", "links"],
    max_depth            = 2,
    branching_factor     = 3,
    max_pages            = 10,
    score_threshold      = 0.1,
    word_count_threshold = 10,
    # url_pattern        = "https://www.tensorflow.org/tutorials",
    exclude_social_media = True,
    respect_robots_txt   = True,
    politeness_delay     = 0.5,   # seconds between requests
    debug                = False,
    tracker              = tracker,
)

# -- save to files ------------------------------------------------------------
if SAVE_FILES:
    save_results(
        results,
        output_dir = OUTPUT_DIR,
        formats    = ["markdown"],
        # run_label= "tf_tutorials",   # uncomment for a custom folder name
    )

# -- display ------------------------------------------------------------------
print_results(results, formats=["markdown"])

# print_results(results)                          # all formats
# print_results(results, formats=["metadata", "links"])
# print_metadata(results)
# print_links(results, max_show=10)
# print_markdown(results, preview=2000)
# print_cleaned_html(results, preview=1000)
# print_results(results[0], formats=["markdown"]) # single page
