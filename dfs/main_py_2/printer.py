"""
printer.py — display crawl results in a readable format.

Usage
-----
from printer import print_results, print_metadata, print_links, print_markdown, print_cleaned_html

# print all formats for all pages
print_results(results)

# print specific formats only
print_results(results, formats=["metadata", "links"])

# print a single format directly
print_metadata(results)
print_links(results)
print_markdown(results, preview=2000)
print_cleaned_html(results, preview=1000)

# single page
print_results(results[0])
"""

SEP  = "=" * 70
SEP2 = "-" * 70


# ── individual format printers ─────────────────────────────────────────────────

def print_metadata(results):
    for i, r in enumerate(_as_list(results), 1):
        _page_header(i, len(_as_list(results)), r)
        if r.error:
            _error(r); continue
        print(f"\n{'[ METADATA ]':^70}")
        print(SEP2)
        if r.metadata:
            for key, val in r.metadata.items():
                print(f"  {key:<14}: {val}")
        else:
            print("  (none)")
        print()


def print_links(results, max_show=5):
    for i, r in enumerate(_as_list(results), 1):
        _page_header(i, len(_as_list(results)), r)
        if r.error:
            _error(r); continue
        print(f"\n{'[ LINKS ]':^70}")
        print(SEP2)
        if r.links:
            internal = [l for l in r.links if l["internal"]]
            external = [l for l in r.links if not l["internal"]]
            print(f"  Total: {len(r.links)}  |  Internal: {len(internal)}  |  External: {len(external)}")

            if internal:
                print(f"\n  Internal (first {max_show}):")
                for l in internal[:max_show]:
                    print(f"    {l['text'][:45]:<45}  {l['href']}")

            if external:
                print(f"\n  External (first {max_show}):")
                for l in external[:max_show]:
                    print(f"    {l['text'][:45]:<45}  {l['href']}")
        else:
            print("  (none)")
        print()


def print_markdown(results, preview=1500):
    for i, r in enumerate(_as_list(results), 1):
        _page_header(i, len(_as_list(results)), r)
        if r.error:
            _error(r); continue
        print(f"\n{'[ MARKDOWN ]':^70}")
        print(SEP2)
        if r.markdown:
            print(r.markdown[:preview])
            if len(r.markdown) > preview:
                print(f"\n  ... [{len(r.markdown) - preview} more chars "
                      f"— {len(r.markdown.split())} total words]")
        else:
            print("  (none)")
        print()


def print_cleaned_html(results, preview=1500):
    for i, r in enumerate(_as_list(results), 1):
        _page_header(i, len(_as_list(results)), r)
        if r.error:
            _error(r); continue
        print(f"\n{'[ CLEANED HTML ]':^70}")
        print(SEP2)
        if r.cleaned_html:
            print(r.cleaned_html[:preview])
            if len(r.cleaned_html) > preview:
                print(f"\n  ... [{len(r.cleaned_html) - preview} more chars]")
        else:
            print("  (none)")
        print()


def print_raw_html(results, preview=1500):
    for i, r in enumerate(_as_list(results), 1):
        _page_header(i, len(_as_list(results)), r)
        if r.error:
            _error(r); continue
        print(f"\n{'[ RAW HTML ]':^70}")
        print(SEP2)
        if r.raw_html:
            print(r.raw_html[:preview])
            if len(r.raw_html) > preview:
                print(f"\n  ... [{len(r.raw_html) - preview} more chars]")
        else:
            print("  (none)")
        print()


# ── combined printer ───────────────────────────────────────────────────────────

# maps format name → (printer_function, extra_kwargs)
_PRINTERS = {
    "metadata":     (print_metadata,     {}),
    "links":        (print_links,        {}),
    "markdown":     (print_markdown,     {}),
    "cleaned_html": (print_cleaned_html, {}),
    "raw_html":     (print_raw_html,     {}),
}

VALID_FORMATS = set(_PRINTERS.keys())


def print_results(results, formats=None, preview=1500, max_links=5):
    """
    Print crawl results.

    Parameters
    ----------
    results  : list[PageResult] or single PageResult
    formats  : list of format names to print, e.g. ["metadata", "markdown"]
               None → print all formats present on the results
    preview  : max chars to show for markdown / html outputs
    max_links: max links to show per section in links output
    """
    results = _as_list(results)

    # if formats not specified, auto-detect from first non-error result
    if formats is None:
        formats = _detect_formats(results)

    unknown = set(formats) - VALID_FORMATS
    if unknown:
        raise ValueError(f"Unknown format(s): {unknown}. Valid: {VALID_FORMATS}")

    for i, r in enumerate(results, 1):
        print(f"\n{SEP}")
        print(f"  PAGE {i}/{len(results)}")
        print(f"  URL    : {r.url}")
        print(f"  depth  : {r.depth}  |  status : {r.status_code}")
        print(SEP)

        if r.error:
            _error(r)
            continue

        for fmt in formats:
            if fmt == "metadata" and r.metadata:
                print(f"\n{'[ METADATA ]':^70}")
                print(SEP2)
                for key, val in r.metadata.items():
                    print(f"  {key:<14}: {val}")

            elif fmt == "links" and r.links:
                internal = [l for l in r.links if l["internal"]]
                external = [l for l in r.links if not l["internal"]]
                print(f"\n{'[ LINKS ]':^70}")
                print(SEP2)
                print(f"  Total: {len(r.links)}  |  Internal: {len(internal)}  |  External: {len(external)}")
                if internal:
                    print(f"\n  Internal (first {max_links}):")
                    for l in internal[:max_links]:
                        print(f"    {l['text'][:45]:<45}  {l['href']}")
                if external:
                    print(f"\n  External (first {max_links}):")
                    for l in external[:max_links]:
                        print(f"    {l['text'][:45]:<45}  {l['href']}")

            elif fmt == "markdown" and r.markdown:
                print(f"\n{'[ MARKDOWN ]':^70}")
                print(SEP2)
                print(r.markdown[:preview])
                if len(r.markdown) > preview:
                    print(f"\n  ... [{len(r.markdown) - preview} more chars "
                          f"— {len(r.markdown.split())} total words]")

            elif fmt == "cleaned_html" and r.cleaned_html:
                print(f"\n{'[ CLEANED HTML ]':^70}")
                print(SEP2)
                print(r.cleaned_html[:preview])
                if len(r.cleaned_html) > preview:
                    print(f"\n  ... [{len(r.cleaned_html) - preview} more chars]")

            elif fmt == "raw_html" and r.raw_html:
                print(f"\n{'[ RAW HTML ]':^70}")
                print(SEP2)
                print(r.raw_html[:preview])
                if len(r.raw_html) > preview:
                    print(f"\n  ... [{len(r.raw_html) - preview} more chars]")

        print()


# ── helpers ────────────────────────────────────────────────────────────────────

def _as_list(results):
    """Accept a single PageResult or a list."""
    from outputs import PageResult
    return [results] if isinstance(results, PageResult) else list(results)


def _detect_formats(results):
    """Return formats that have data in at least one result."""
    present = []
    for fmt in ["metadata", "links", "markdown", "cleaned_html", "raw_html"]:
        if any(getattr(r, fmt, None) is not None for r in results):
            present.append(fmt)
    return present


def _page_header(i, total, r):
    print(f"\n{SEP}")
    print(f"  PAGE {i}/{total}  |  {r.url}")
    print(f"  depth : {r.depth}  |  status : {r.status_code}")
    print(SEP)


def _error(r):
    print(f"  ERROR: {r.error}\n")
