"""
saver.py — saves crawl results to disk.

Creates one folder per crawl run, one subfolder per page.
Each requested format is written as a separate file.

Folder structure
----------------
output_dir/
  crawl_20240816_143022/        <- timestamped run folder
    index.json                  <- summary of all pages crawled
    page_0_tutorials/
      metadata.json
      markdown.md
      cleaned.html
      raw.html
      links.json
    page_1_beginner/
      ...

Usage
-----
from saver import save_results

save_results(results)                           # saves to ./crawl_output/
save_results(results, output_dir="my_crawl")   # custom root folder
save_results(results, formats=["markdown"])     # specific formats only
save_results(results, run_label="tf_docs")      # custom run folder name
"""

import json
import os
import re
from datetime import datetime
from typing import Optional


# maps PageResult field name -> filename on disk
_FORMAT_FILES = {
    "markdown":     "markdown.md",
    "cleaned_html": "cleaned.html",
    "raw_html":     "raw.html",
    "metadata":     "metadata.json",
    "links":        "links.json",
}


def save_results(
    results,
    output_dir: str = "crawl_output",
    formats: Optional[list] = None,
    run_label: Optional[str] = None,
) -> str:
    """
    Save crawl results to disk.

    Parameters
    ----------
    results    : list[PageResult] or single PageResult
    output_dir : root folder.
                 If a relative path is given it resolves from the current
                 working directory (i.e. wherever you launched Python from).
                 Pass an absolute path to be explicit, e.g.:
                   save_results(results, output_dir="/home/user/crawls")
    formats    : formats to save. None -> all available formats saved.
    run_label  : run folder name. None -> auto timestamped.

    Returns
    -------
    str — absolute path to the run folder created
    """
    from outputs import PageResult
    if isinstance(results, PageResult):
        results = [results]

    # -- create run folder -----------------------------------------------------
    label   = run_label or datetime.now().strftime("crawl_%Y%m%d_%H%M%S")
    run_dir = os.path.join(output_dir, label)
    os.makedirs(run_dir, exist_ok=True)

    # -- resolve formats -------------------------------------------------------
    if formats is None:
        # auto-detect: only save formats that have data in at least one result
        formats = [
            fmt for fmt in _FORMAT_FILES
            if any(getattr(r, fmt, None) is not None for r in results)
        ]

    unknown = set(formats) - set(_FORMAT_FILES)
    if unknown:
        raise ValueError(f"Unknown format(s): {unknown}. Valid: {set(_FORMAT_FILES)}")

    # -- write each page -------------------------------------------------------
    index = []

    for i, r in enumerate(results):
        slug     = _url_to_slug(r.url)
        page_dir = os.path.join(run_dir, f"page_{i}_{slug}")
        os.makedirs(page_dir, exist_ok=True)

        entry = {
            "index":       i,
            "url":         r.url,
            "depth":       r.depth,
            "status_code": r.status_code,
            "error":       r.error,
            "files":       {},
        }

        if r.error:
            _write(os.path.join(page_dir, "error.txt"), r.error)
            entry["files"]["error"] = "error.txt"
            index.append(entry)
            continue

        for fmt in formats:
            value = getattr(r, fmt, None)
            if value is None:
                continue

            filename = _FORMAT_FILES[fmt]
            content  = (
                json.dumps(value, indent=2, ensure_ascii=False)
                if fmt in ("metadata", "links")
                else value
            )
            _write(os.path.join(page_dir, filename), content)
            entry["files"][fmt] = filename

        index.append(entry)

    # -- write index.json ------------------------------------------------------
    _write(
        os.path.join(run_dir, "index.json"),
        json.dumps({
            "run":        label,
            "total":      len(results),
            "successful": sum(1 for r in results if not r.error),
            "formats":    formats,
            "pages":      index,
        }, indent=2, ensure_ascii=False)
    )

    abs_path = os.path.abspath(run_dir)
    print(f"[saver] {len(results)} pages -> {abs_path}")
    print(f"[saver] formats: {formats}")
    return abs_path


# -- helpers -------------------------------------------------------------------

def _write(path: str, content: str):
    # page_dir is already created by the caller; no redundant makedirs needed.
    # We keep it here only as a safety net for edge cases (e.g. index.json at
    # the run_dir level which is also pre-created).
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _url_to_slug(url: str, max_len: int = 40) -> str:
    slug = re.sub(r"^https?://", "", url)
    slug = re.sub(r"[^\w]", "_", slug)
    slug = re.sub(r"_+", "_", slug).strip("_")
    return slug[:max_len]
