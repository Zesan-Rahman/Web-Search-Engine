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

from classes import LinkExtractor, Logger

# Constants
PRIO = 0
LINK = 1
DEPTH = 2
MAX_CRAWL = 5100

BLACKLIST_EXTENSIONS = ('.jpg', '.jpeg', '.png', '.gif', '.pdf', '.zip',
                         '.mp3', '.mp4', '.css', '.js', '.svg', '.ico')

INDEX_NAMES = ('index.htm', 'index.html', 'index.jsp', 'index.php',
               'main.html', 'default.htm', 'default.html')

socket.setdefaulttimeout(3)

# Dictionaries + locks
domain_counts = {}
superdomain_counts = {}
domains_lock = threading.Lock()

visited_links = {}
visited_lock = threading.Lock()

robot_domains = {}
robots_lock = threading.Lock()

domain_latency = {}  
latency_lock = threading.Lock()

log_lock = threading.Lock()
stats_lock = threading.Lock()
filecount_lock = threading.Lock()
response_code_counts = {}      
total_size = [0]
total_crawled = [0]
crawl_done = threading.Event()


#Log each crawled page
def log_page(logger: Logger, url: str, code: int, size: int, depth: int) -> None:
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    logger.write(f"{timestamp}\t{code}\t{size}\t{depth}\t{url}")
    with stats_lock:
        response_code_counts[code] = response_code_counts.get(code, 0) + 1
        total_size[0] += size
        total_crawled[0] += 1
        if total_crawled[0] >= MAX_CRAWL:
            crawl_done.set()

'''
My solution to url forwarding was normalizing all urls by 
1) Chopping off port numbers 
2) Chopping off index names (e.g. example.com/foo/index.html = example.com/foo)
3) Strip any trailing slash (e.g. example.com/foo/ = example.com/foo)
4) Chop any fragments
Note: 
www.example.com and example.com are treated as different urls. 
'''
def normalize_url(url: str) -> str:
    parts = urlsplit(url)

    scheme = parts.scheme.lower()

    #Chop off ports
    netloc = parts.netloc.lower()
    if scheme == 'http' and netloc.endswith(':80'):
        netloc = netloc[:-3]
    elif scheme == 'https' and netloc.endswith(':443'):
        netloc = netloc[:-4]

    #Chop off index names 
    path = parts.path
    for name in INDEX_NAMES:
        suffix = '/' + name
        if path.endswith(suffix):
            path = path[: -len(name)]
            break

    #Strip trailing slash
    if len(path) > 1 and path.endswith('/'):
        path = path.rstrip('/')
    if path == '':
        path = '/'
    
    #Organize queries alphabetically
    if parts.query:
        query_pairs = sorted(parse_qsl(parts.query, keep_blank_values=True))
        query = urlencode(query_pairs)
    else:
        query = ''

    # always dropped
    fragment = ''  

    return urlunsplit((scheme, netloc, path, query, fragment))

'''
Priority calculation to organize prioqueue
Prio = domain * superdomain * latency of domain 
Domain = 1/log(d+1)
Superdomain = 1/log(sp+1)
latency = record_latency
'''
def calculatePrio(url: str) -> float:
    ext = tldextract.extract(url)
    domain = ext.fqdn
    superdomain = ext.top_domain_under_public_suffix

    with domains_lock:
        domain_counts[domain] = domain_counts.get(domain, 0) + 1
        superdomain_counts[superdomain] = superdomain_counts.get(superdomain, 0) + 1
        p = domain_counts[domain]
        sp = superdomain_counts[superdomain]
    
    with latency_lock:
        avg_latency = domain_latency.get(domain, 0)

    domain_term = 1 / log(p + 1)
    superdomain_term = 1 / log(sp + 1)
    latency_term = 1 / (1 + avg_latency)

    prio = domain_term * superdomain_term * latency_term
    return prio

def record_latency(domain: str, elapsed: float) -> None:
    with latency_lock:
        if domain not in domain_latency:
            domain_latency[domain] = elapsed
        else:
            alpha = 0.3  # weight toward recent measurements
            domain_latency[domain] = alpha * elapsed + (1 - alpha) * domain_latency[domain]

# Robots.txt: if no robots.txt assume page can be crawled 
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
        rp = None  

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

'''
Each page is downloaded to content/
Pretty much all of them start with 200. Why did i keep it? idk
Returns html and size of html
'''
def download_page(response, fileCount: int) -> tuple[str, int] | None:
    raw_bytes = response.read()
    size = len(raw_bytes)
    html = raw_bytes.decode("utf-8", errors='replace')
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    fileName = "content/" + str(response.status) + "-" + str(fileCount) + "-" + timestamp
    try:
        with open(fileName, 'x', encoding='utf-8') as f:
            f.write(html)
    except OSError as e:
        print(f"Failed to write {fileName}: {e}")
        return None
    return html, size

'''
Uses the linkextractor class to find and return a list of links
'''
def links_from_page(base_url: str, html: str) -> List[str]:
    parser = LinkExtractor()
    #Feed uses handle start tag, hence the override
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

'''
Page parser function
1) Checks robot.txt. If not allowed, return
2) Send request to server with a timeout of 5 seconds. 
A lot of low-level exceptions within the request library can rise from this.
I chose to decide they meant I wasn't allowed to parse (but then why didn't they have robots.txt?)
3) Normalize the url and check if we visited this before. 
4) Ensure the page is an html page. if it isn't, return
5) Check filecount and increment
6) Download page
7) Get links from page and push to q
'''
def parse_url(fileCount, url: str, depth: int, q: queue.PriorityQueue, logger: Logger) -> None:
    rp = get_robot_parser(url)
    if rp is not None and not rp.can_fetch("*", url):
        log_page(logger, url, 404, 0, depth)
        return

    domain = tldextract.extract(url).fqdn
    start = time.time()

    try:
        response = request.urlopen(url, timeout=3)
    except HTTPError as e:
        # log_page(logger, url, e.code, 0, depth)
        return
    except URLError as e:
        # logger.write(f"Failed to reach server for {url}")
        return
        #Catch low level exceptions killing my threads
    except Exception as e:
        # logger.write(f"Low-level connection error on {url}: {e}")
        # print(f"Low-level connection error on {url}: {e}")
        return

    record_latency(domain, time.time() - start)

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

'''
Setup: 
1) Search query
1.5) Sanitize search query
2) creates content directory and log files
3) Create logfile based on current time and query
3.5) Add some details about the query and log file format
4) Search up query off google.com
5) pushes first 10 links to q
6) return logger
'''
def setup(q: queue.PriorityQueue) -> Logger | None:

    print("Enter search query:")
    user_query = input()
    user_query = "".join(c if c.isalnum() else "-" for c in user_query)
    
    if "content" not in os.listdir("."):
        os.mkdir("content")

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

'''
Worker thread function:
while crawl not done:
1) pop from q
2) parse_url
2.5) log any errors
'''
def worker(q, fileCount, logger: Logger) -> None:
    while not crawl_done.is_set():
        try:
            url_info = q.get(timeout=3)
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

'''
Monitor thread function: Prints stats every 10 seconds about total download count
'''
def monitor(start_time: float, stop_event: threading.Event, q: queue.PriorityQueue, interval: float = 10) -> None:
    count = 0
    while not stop_event.wait(interval):
        elapsed = time.time() - start_time
        with stats_lock:
            new_count = total_crawled[0]
        rate = (new_count - count) / interval if interval > 0 else 0
        count = new_count
        print(f"[{elapsed:.0f}s] {count} total pages crawled ({rate:.2f}/sec) | "
            f"active threads: {threading.active_count()} | queue size: {q.qsize()}")

'''
Basically main:
1) Create prioqueue
2) Setup and obtain logger
2.5) If setup malfunctioned, return
3) Create and fire worker threads and monitor thread
4) Log final results
'''
def crawler(num_threads: int = 15, log_file_name: str = "log") -> None:
    q = queue.PriorityQueue()
    logger = setup(q)
    if logger is None:
        return

    fileCount = itertools.count(0)
    start_time = time.time()

    stop_event = threading.Event()
    mon = threading.Thread(target=monitor, args=(start_time, stop_event, q))
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
    #For some reason, None gets pushed as a code to response_code_counts sometimes. This takes care of it?
    for code, cnt in sorted(response_code_counts.items(), key=lambda x: (x[0] is None, x[0])):
        logger.write(f"Code {code}: {cnt}")

    logger.close()
    # print(f"Crawled {total_crawled} pages in {elapsed:.2f}s ({total_crawled/elapsed:.2f} pages/sec)")

if __name__ == "__main__":
    crawler()
