import os
import queue
import threading
import itertools
import time
from math import log
from urllib import request
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse, urljoin, urlsplit, urlunsplit, parse_qsl, urlencode
from urllib.robotparser import RobotFileParser
from html.parser import HTMLParser
from typing import Tuple, List

import tldextract
from ddgs import DDGS

# Constants
PRIO = 0
LINK = 1
DEPTH = 2

BLACKLIST_EXTENSIONS = ('.jpg', '.jpeg', '.png', '.gif', '.pdf', '.zip',
                         '.mp3', '.mp4', '.css', '.js', '.svg', '.ico')

INDEX_NAMES = ('index.htm', 'index.html', 'index.jsp', 'index.php',
               'main.html', 'default.htm', 'default.html')

# Shared state + locks

domain_counts = {}
superdomain_counts = {}
domains_lock = threading.Lock()

visited_links = {}
visited_lock = threading.Lock()

robot_domains = {}
robots_lock = threading.Lock()

filecount_lock = threading.Lock()

pages_crawled = [0]
pages_crawled_lock = threading.Lock()

# HTML link extraction
class LinkExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
        self.base_href = None

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            for name, value in attrs:
                if name == "href" and value:
                    self.links.append(value)
        elif tag == "base" and self.base_href is None:
            for name, value in attrs:
                if name == "href" and value:
                    self.base_href = value


# URL normalization

def normalize_url(url: str) -> str:
    parts = urlsplit(url)

    scheme = parts.scheme.lower()

    netloc = parts.netloc.lower()  # www. is intentionally left as-is (treated as distinct)
    if scheme == 'http' and netloc.endswith(':80'):
        netloc = netloc[:-3]
    elif scheme == 'https' and netloc.endswith(':443'):
        netloc = netloc[:-4]

    path = parts.path
    for name in INDEX_NAMES:
        suffix = '/' + name
        if path.endswith(suffix):
            path = path[: -len(name)]
            break

    if len(path) > 1 and path.endswith('/'):
        path = path.rstrip('/')
    if path == '':
        path = '/'

    if parts.query:
        query_pairs = sorted(parse_qsl(parts.query, keep_blank_values=True))
        query = urlencode(query_pairs)
    else:
        query = ''

    fragment = ''  # always dropped

    return urlunsplit((scheme, netloc, path, query, fragment))


# Priority calculation
def calculatePrio(url: str) -> float:
    ext = tldextract.extract(url)
    domain = ext.fqdn
    superdomain = ext.top_domain_under_public_suffix

    with domains_lock:
        domain_counts[domain] = domain_counts.get(domain, 0) + 1
        superdomain_counts[superdomain] = superdomain_counts.get(superdomain, 0) + 1
        p = domain_counts[domain]
        sp = superdomain_counts[superdomain]

    domain_term = 1 / log(p + 1)
    superdomain_term = 1 / log(sp + 1)

    prio = domain_term * superdomain_term
    return prio

# Robots.txt handling
def get_robot_parser(url: str) -> RobotFileParser | None:
    parsed = urlparse(url)
    domain = tldextract.extract(url).fqdn

    with robots_lock:
        if domain in robot_domains:
            return robot_domains[domain]

    rp = RobotFileParser()
    rp.set_url(f"{parsed.scheme}://{domain}/robots.txt")
    try:
        rp.read()
    except Exception:
        rp = None  # treat missing/unreachable robots.txt as no restrictions

    with robots_lock:
        robot_domains[domain] = rp
    return rp

# Queueing links
def push_links(q: "queue.PriorityQueue", links: List[str], depth: int) -> None:
    for link in links:
        link = normalize_url(link)

        with visited_lock:
            if link in visited_links:
                continue

        rp = get_robot_parser(link)
        if rp is not None and not rp.can_fetch("*", link):
            continue

        item = (-1 * calculatePrio(link), link, depth)
        q.put(item)

def download_page(response, fileCount: int) -> str | None:
    html = response.read().decode("utf-8", errors='replace')
    fileName = "content/" + str(response.status) + "-" + str(fileCount) + ".parsed"
    try:
        with open(fileName, 'x', encoding='utf-8') as f:
            f.write(html)
    except OSError as e:
        print(f"Failed to write {fileName}: {e}")
        return None
    return html

def links_from_page(base_url: str, html: str) -> List[str]:
    parser = LinkExtractor()
    parser.feed(html)

    link_base_url = base_url
    if parser.base_href:
        link_base_url = urljoin(base_url, parser.base_href)

    resolved_links = []
    for link in parser.links:
        if link.lower().endswith(BLACKLIST_EXTENSIONS):
            continue
        full_link = urljoin(link_base_url, link)
        resolved_links.append(full_link)

    return resolved_links

# Fetching + parsing a single page
def parse_url(fileCount, url: str, depth: int, q: "queue.PriorityQueue") -> None:
    try:
        response = request.urlopen(url)
    except HTTPError as e:
        print('The server couldn\'t fulfill the request.')
        print('Error code: ', e.code)
        return
    except URLError as e:
        print('We failed to reach a server.')
        print('Reason: ', e.reason)
        return

    base_url = normalize_url(response.url)  # final URL after any redirects
    with visited_lock:
        if base_url in visited_links:
            return
        visited_links[base_url] = 1

    content_type = response.headers.get_content_type()
    if content_type != "text/html":
        print(f"Skipping non-HTML content ({content_type}) at {url}")
        return

    with filecount_lock:
        current_filecount = next(fileCount)

    html = download_page(response, current_filecount)
    if not html:
        return

    with pages_crawled_lock:
        pages_crawled[0] += 1

    new_links = links_from_page(base_url, html)
    push_links(q, new_links, depth + 1)

# Setup (search query -> seed links)
def setup(q: "queue.PriorityQueue") -> None:
    if "content" not in os.listdir("."):
        os.mkdir("content")

    print("Enter search query:")
    user_query = input()
    print(f"Searching {user_query} using google.com")
    results = DDGS().text(user_query, region='us-en', safesearch='on')
    links = [result['href'] for result in results]
    print(f"First 10 links: {links}")
    push_links(q, links, 0)

# Worker + monitor threads
def worker(q: "queue.PriorityQueue", fileCount) -> None:
    while True:
        try:
            url_info = q.get(timeout=5)
        except queue.Empty:
            return
        url = url_info[LINK]
        depth = url_info[DEPTH]
        print(f"parsing {url}")
        parse_url(fileCount, url, depth, q)


def monitor(start_time: float, stop_event: threading.Event, interval: float = 5) -> None:
    while not stop_event.wait(interval):
        elapsed = time.time() - start_time
        with pages_crawled_lock:
            count = pages_crawled[0]
        rate = count / elapsed if elapsed > 0 else 0
        print(f"[{elapsed:.0f}s] {count} pages crawled ({rate:.2f}/sec)")

# Main
def crawler(num_threads: int = 15) -> None:
    q = queue.PriorityQueue()
    setup(q)
    fileCount = itertools.count(0)
    start_time = time.time()

    stop_event = threading.Event()
    mon = threading.Thread(target=monitor, args=(start_time, stop_event))
    mon.start()

    threads = [threading.Thread(target=worker, args=(q, fileCount)) for _ in range(num_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    stop_event.set()
    mon.join()

    elapsed = time.time() - start_time
    print(f"Crawled {pages_crawled[0]} pages in {elapsed:.2f}s ({pages_crawled[0]/elapsed:.2f} pages/sec)")


if __name__ == "__main__":
    crawler()
