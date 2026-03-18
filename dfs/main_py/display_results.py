from crawler import crawl
from printer import print_results, print_metadata, print_links, print_markdown, print_cleaned_html

results = crawl(
    start_url            = "https://www.tensorflow.org/tutorials",
    output_formats       = ["cleaned_html", "markdown", "metadata", "links"],
    max_depth            = 2,
    branching_factor     = 3,
    max_pages            = 3,
    score_threshold      = 0.3,
    word_count_threshold = 50,
    url_pattern          = "https://www.tensorflow.org/tutorials",
    exclude_external     = True,
    exclude_social_media = True,
    debug                = False,
)

# ── print all formats (auto-detected from results)
# print_results(results)

# ── print specific formats only
# print_results(results, formats=["metadata", "links"])

# ── print one format across all pages
# print_metadata(results)
# print_links(results, max_show=10)
# print_markdown(results, preview=2000)
# print_cleaned_html(results, preview=1000)

# ── single page
print_results(results[0], formats=["markdown"])