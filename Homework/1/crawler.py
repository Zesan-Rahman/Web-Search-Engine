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
from typing import List
import tldextract
from ddgs import DDGS
import socket

# Constants
PRIO = 0
LINK = 1
DEPTH = 2
MAX_CRAWL = 5100

BLACKLIST_EXTENSIONS = ('.jpg', '.jpeg', '.png', '.gif', '.pdf', '.zip',
                         '.mp3', '.mp4', '.css', '.js', '.svg', '.ico')

INDEX_NAMES = ('index.htm', 'index.html', 'index.jsp', 'index.php',
               'main.html', 'default.htm', 'default.html')

socket.setdefaulttimeout(5)

# Dictionaries + locks
domain_counts = {}
superdomain_counts = {}
domains_lock = threading.Lock()

visited_links = {}
visited_lock = threading.Lock()

robot_domains = {}
robots_lock = threading.Lock()

log_lock = threading.Lock()
stats_lock = threading.Lock()
filecount_lock = threading.Lock()
response_code_counts = {}      
total_size = [0]
total_crawled = [0]
crawl_done = threading.Event()

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

class Logger:
    def __init__(self, filename: str):
        self.file = open(filename, 'a', buffering=1, encoding='utf-8')
        self.lock = threading.Lock()

    def write(self, message: str) -> None:
        with self.lock:
            try:
                self.file.write(message + "\n")
            except OSError as e:
                print(f"Failed to write to log file: {e}")

    def close(self) -> None:
        with self.lock:
            self.file.close()

def log_page(logger: Logger, url: str, code: int, size: int, depth: int) -> None:
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    logger.write(f"{timestamp}\t{code}\t{size}\t{depth}\t{url}")
    with stats_lock:
        response_code_counts[code] = response_code_counts.get(code, 0) + 1
        total_size[0] += size
        total_crawled[0] += 1
        if total_crawled[0] >= MAX_CRAWL:
            crawl_done.set()

#www counts as a new url (www.example.com and example.com are distinct)
def normalize_url(url: str) -> str:
    parts = urlsplit(url)

    scheme = parts.scheme.lower()

    netloc = parts.netloc.lower()
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
def push_links(q: queue.PriorityQueue, links: List[str], depth: int) -> None:
    for link in links:
        if "cgi" in link.lower():
            continue
        link = normalize_url(link)
        with visited_lock:
            if link in visited_links:
                continue

        item = (-1 * calculatePrio(link), link, depth)
        q.put(item)

def download_page(response, fileCount: int) -> tuple[str, int] | None:
    raw_bytes = response.read()
    size = len(raw_bytes)
    html = raw_bytes.decode("utf-8", errors='replace')
    fileName = "content/" + str(response.status) + "-" + str(fileCount)
    try:
        with open(fileName, 'x', encoding='utf-8') as f:
            f.write(html)
    except OSError as e:
        print(f"Failed to write {fileName}: {e}")
        return None
    return html, size

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
def parse_url(fileCount, url: str, depth: int, q: queue.PriorityQueue, logger: Logger) -> None:
    rp = get_robot_parser(url)
    if rp is not None and not rp.can_fetch("*", url):
        log_page(logger, url, 404, 0, depth)
        return

    try:
        response = request.urlopen(url, timeout=5)
    except HTTPError as e:
        log_page(logger, url, e.code, 0, depth)
        return
    except URLError as e:
        logger.write(f"Failed to reach server for {url}")
        return
        #Catch low level exceptions killing my threads
    except Exception as e:
        logger.write(f"Low-level connection error on {url}: {e}")
        print(f"Low-level connection error on {url}: {e}")
        return

    base_url = normalize_url(response.url)
    with visited_lock:
        if base_url in visited_links:
            return
        visited_links[base_url] = 1

    content_type = response.headers.get_content_type()
    if content_type != "text/html":
        # print(f"Skipping non-HTML content ({content_type}) at {url}")
        log_page(logger, base_url, response.status, 0, depth)
        return

    with filecount_lock:
        current_filecount = next(fileCount)

    result = download_page(response, current_filecount)
    if not result:
        return
    html, size = result

    log_page(logger, base_url, response.status, size, depth)

    new_links = links_from_page(base_url, html)
    push_links(q, new_links, depth + 1)

# Setup: Search query, pushes first 10 links, creates content directory and log files
def setup(q: queue.PriorityQueue) -> Logger | None:

    print("Enter search query:")
    user_query = input()
    user_query = "".join(c if c.isalnum() else "-" for c in user_query)
    
    dir = "content/" + user_query
    if "content" not in os.listdir("."):
        os.mkdir("content")
    if dir not in os.listdir("./content"):
        os.mkdir(dir)
    else:
        print("You have crawled off this query before and should remove its corresponding directory in content/")
        return

    timestamp = time.strftime("%Y%m%d_%H%M%S")
    log_file_name = user_query + "_" + timestamp + "_results.txt"
    logger = Logger(log_file_name)
    logger.write(f"Searching {user_query} using google.com")
    logger.write("Format: timestamp code size depth url")

    results = DDGS().text(user_query, region='us-en', safesearch='on')
    links = [result['href'] for result in results]
    # print(f"First 10 links: {links}")

    push_links(q, links, 0)
    return logger

# Worker + monitor threads
def worker(q, fileCount, logger: Logger) -> None:
    while not crawl_done.is_set():
        try:
            url_info = q.get(timeout=5)
        except queue.Empty:
            return
        url = url_info[LINK]
        depth = url_info[DEPTH]
        # print(f"parsing {url}")
        try:
            parse_url(fileCount, url, depth, q, logger)
        except Exception as e:
            logger.write(f"Unknown error with {url}")
    return

def monitor(start_time: float, stop_event: threading.Event, interval: float = 5) -> None:
    count = 0
    while not stop_event.wait(interval):
        elapsed = time.time() - start_time
        new_count = len(os.listdir("./content/"))
        rate = (new_count - count) / elapsed if elapsed > 0 else 0
        count = new_count
        print(f"[{elapsed:.0f}s] {count} total pages crawled ({rate:.2f}/sec) in the last 10 seconds")

# Main
def crawler(num_threads: int = 15, log_file_name: str = "log") -> None:
    q = queue.PriorityQueue()
    logger = setup(q)
    if logger is None:
        return

    fileCount = itertools.count(0)
    start_time = time.time()

    stop_event = threading.Event()
    mon = threading.Thread(target=monitor, args=(start_time, stop_event))
    mon.start()

    threads = [threading.Thread(target=worker, args=(q, fileCount, logger)) for _ in range(num_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    stop_event.set()
    mon.join()

    elapsed = time.time() - start_time
    logger.write("")
    logger.write(f"Pages crawled: {total_crawled[0]}")
    logger.write(f"Total size: {total_size[0]} bytes")
    logger.write(f"Total time: {elapsed:.2f}s")
    for code, cnt in sorted(response_code_counts.items()):
        logger.write(f"Code {code}: {cnt}")

    logger.close()
    # print(f"Crawled {total_crawled} pages in {elapsed:.2f}s ({total_crawled/elapsed:.2f} pages/sec)")

if __name__ == "__main__":
    crawler()
