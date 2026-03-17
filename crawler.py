import requests
import heapq
from bs4 import BeautifulSoup
from collections import deque
from urllib.parse import urljoin, urlparse, urlunparse

# Set to keep track of visited URLs
visited = set()

def normalize_and_filter_url(base_url, current_url, href):
    """Normalize URL and filter out external, duplicate, or non-HTML resources."""
    # Convert to absolute URL
    absolute_url = urljoin(current_url, href)
    parsed = urlparse(absolute_url)

    # Remove fragment (#...) and query params (?hl=en)
    parsed = parsed._replace(fragment="", query="")

    # Normalize path (remove trailing slash)
    normalized_path = parsed.path.rstrip("/")
    parsed = parsed._replace(path=normalized_path)

    clean_url = urlunparse(parsed)

    # Same domain check
    if urlparse(clean_url).netloc != urlparse(base_url).netloc:
        return None

    # Restrict to base path
    if not clean_url.startswith(base_url.rstrip("/")):
        return None

    # Skip non-html resources
    if clean_url.endswith((".pdf", ".jpg", ".png", ".zip", ".css", ".js")):
        return None

    return clean_url

def clean_html(soup):
    """Clean HTML by removing boilerplate elements like headers, footers, nav, and ads."""
    # 1. Define common boilerplate tags
    boilerplate_tags = ["header", "footer", "nav", "aside", "script", "style", "iframe", "noscript"]
    for tag in soup(boilerplate_tags):
        tag.decompose()

    # 2. Define common boilerplate classes or IDs
    # These are regex patterns that might appear in class or id names
    boilerplate_patterns = ["menu", "footer", "sidebar", "nav", "banner", "social", "ads", "cookie", "popup"]
    
    for tag in soup.find_all(True):  # find_all(True) finds all tags
        tag_id = tag.get("id", "").lower()
        tag_classes = " ".join(tag.get("class", [])).lower()
        
        for pattern in boilerplate_patterns:
            if pattern in tag_id or pattern in tag_classes:
                tag.decompose()
                break

    # 3. Focus on main content if available
    main_content = soup.find("main") or soup.find("article") or soup.find(id="content")
    if main_content:
        # If we found a main section, we can potentially discard everything else
        # For simplicity in this implementation, we return the cleaned main content
        return main_content
    
    return soup

def extract_content(url):
    """Fetch URL and return cleaned soup, text, and clean links."""
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        response = requests.get(url, headers=headers, timeout=5)
        response.raise_for_status()
        
        soup = BeautifulSoup(response.text, "html.parser")
        
        # Extract links BEFORE cleaning (so we don't miss navigation links)
        # OR extract after cleaning if we only want links from the main content
        raw_links = soup.find_all("a", href=True)
        
        # Clean the HTML
        cleaned_soup = clean_html(soup)
        
        # Extract text from cleaned soup
        text = cleaned_soup.get_text(separator=" ", strip=True)
        
        return text, raw_links
    except Exception as e:
        print(f"Error fetching {url}: {e}")
        return None, []

def process_links(base_url, current_url, raw_links, max_links=3):
    """Normalize and filter raw links."""
    clean_links = []
    seen_links = set()
    
    for link in raw_links:
        clean_url = normalize_and_filter_url(base_url, current_url, link["href"])
        
        if clean_url and clean_url not in seen_links and clean_url not in visited:
            seen_links.add(clean_url)
            clean_links.append(clean_url)
            if len(clean_links) >= max_links:
                break
    return clean_links

def dfs_crawl(url, base_url, depth=0, max_depth=3):
    """Depth-First Search Crawl."""
    url = url.rstrip("/")
    if depth > max_depth or url in visited:
        return

    print(f"\n{'  '*depth}[DFS] Visiting: {url}")
    visited.add(url)

    text, raw_links = extract_content(url)
    if text:
        print(f"{'  '*depth}Sample Text: {text[:200]}...")
        
        clean_links = process_links(base_url, url, raw_links)
        for cl in clean_links:
            print(f"{'  '*depth}→ Sub-link: {cl}")
            dfs_crawl(cl, base_url, depth + 1, max_depth)

def bfs_crawl(start_url, base_url, max_depth=2):
    """Breadth-First Search Crawl."""
    queue = deque([(start_url, 0)])
    visited.clear()
    
    while queue:
        url, depth = queue.popleft()
        url = url.rstrip("/")
        
        if depth > max_depth or url in visited:
            continue
            
        print(f"\n{'  '*depth}[BFS] Visiting: {url}")
        visited.add(url)
        
        text, raw_links = extract_content(url)
        if text:
            print(f"{'  '*depth}Sample Text: {text[:200]}...")
            
            if depth < max_depth:
                clean_links = process_links(base_url, url, raw_links)
                for cl in clean_links:
                    print(f"{'  '*depth}→ Queueing: {cl}")
                    queue.append((cl, depth + 1))

def best_first_crawl(start_url, base_url, max_pages=10, priority_keyword=None):
    """Best-First Search Crawl (Priority Queue based on keyword relevance)."""
    # Priority queue stores (priority, url, depth)
    # Lower priority value = higher priority. We'll use -1 * relevance.
    pq = [(0, start_url, 0)]
    visited.clear()
    count = 0
    
    while pq and count < max_pages:
        priority, url, depth = heapq.heappop(pq)
        url = url.rstrip("/")
        
        if url in visited:
            continue
            
        print(f"\n[Best-First] Visiting (Priority {priority}): {url}")
        visited.add(url)
        count += 1
        
        text, raw_links = extract_content(url)
        if text:
            print(f"Sample Text: {text[:200]}...")
            
            clean_links = process_links(base_url, url, raw_links, max_links=5)
            for cl in clean_links:
                # Simple priority: count occurrences of keyword in parent text
                # or just prioritize based on URL containing the keyword
                relevance = 0
                if priority_keyword:
                    relevance += cl.lower().count(priority_keyword.lower()) * 10
                    # Could also fetch and check, but that's expensive for BFS/DFS comparisons
                
                # Push with negative relevance for max-heap behavior using heapq
                heapq.heappush(pq, (-relevance, cl, depth + 1))
                print(f"→ PQ Add: {cl} (Score: {relevance})")

if __name__ == "__main__":
    base = "https://www.tensorflow.org/tutorials"
    
    print("--- STARTING DFS ---")
    dfs_crawl(base, base, max_depth=1)
    
    # print("\n--- STARTING BFS ---")
    # bfs_crawl(base, base, max_depth=1)
    
    # print("\n--- STARTING BEST-FIRST ---")
    # best_first_crawl(base, base, max_pages=5, priority_keyword="keras")
