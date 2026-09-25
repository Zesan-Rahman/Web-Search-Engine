FILES
-----
crawler.py
    The entire crawler: search-query seeding, priority-queue-driven BFS-like
    traversal, HTML parsing, URL normalization, robots.txt compliance,
    multithreaded downloading, and per-page logging with end-of-run
    statistics. This is the only source file in the submission.

Output produced by running crawler.py (not included in the submission,
generated fresh each run):
    content/
        One file per successfully downloaded HTML page, named
        "<http-status>-<file-counter>-<timestamp>" (e.g. "200-134-20260924_143201").
        Contains the raw downloaded HTML.
    <query>_<timestamp>_results.txt
        The crawl log. One tab-separated line per crawled URL:
            timestamp   code   size(bytes)   depth   url
        Ends with a short block of summary statistics: total pages crawled,
        total bytes downloaded, total time taken, and a breakdown of how
        many pages were seen at each HTTP status code.

REQUIREMENTS
------------
- Python 3.10 or newer (the code uses PEP 604 union type hints, e.g.
  "tuple[str, int] | None").
- Third-party packages (install with pip):
      pip install tldextract ddgs
- Internet access. On first use, tldextract may need to fetch a copy of
  the public suffix list; after that it caches it locally.

HOW TO RUN
----------
    python3 crawler.py

The program will prompt:

    Enter search query:

Type a search query and press Enter. The crawler will:
  1. Look up that query via DuckDuckGo and seed the priority queue with
     the first page of results.
  2. Create a "content/" directory (if one does not already exist) to
     hold downloaded pages.
  3. Create a log file named "<sanitized-query>_<timestamp>_results.txt"
     in the current directory.
  4. Spawn a pool of worker threads (15 by default) that pull the
     highest-priority not-yet-visited URL from the queue, download it,
     extract and queue its links, and log the result.
  5. Stop once MAX_CRAWL pages (5100, a small safety margin above the
     5000-page target) have been logged, or the queue runs dry.

There are no command-line arguments; the only runtime input is the
search query typed at the prompt.

CONFIGURATION / TUNABLE CONSTANTS
----------------------------------
These are set as constants near the top of crawler.py, or as default
function parameters, rather than exposed as command-line flags:

  MAX_CRAWL (default 5100)
      Crawl stops once this many pages have been logged (this counts
      every URL that received a definite HTTP response -- successful
      downloads as well as error codes like 404 -- not only successful
      downloads). Because several worker threads may be mid-request when
      the limit is reached, the actual total can overshoot this number
      slightly.

  crawler(num_threads=15, ...)
      Number of worker threads in the pool. Passed as a parameter to the
      crawler() function; there is no command-line flag for it, so
      changing it currently requires editing the call at the bottom of
      the file (`crawler()`).

  socket.setdefaulttimeout(5)
      Global default timeout (seconds) applied to any socket operation
      that doesn't specify its own timeout, including robots.txt fetches.
      The main page-fetch call also explicitly passes timeout=5.

  BLACKLIST_EXTENSIONS
      File extensions skipped before ever requesting a link (images,
      video, audio, stylesheets, scripts, etc).

  INDEX_NAMES
      Filenames (index.html, main.html, etc.) stripped from the end of a
      URL's path during normalization, so that e.g. "/foo/index.html"
      and "/foo/" are treated as the same page.

LIMITATIONS
-----------
- content/ is not cleared between runs. Running the crawler more than
  once from the same working directory will leave old downloaded pages
  mixed in with the new run's output. Move or delete content/ manually
  between runs if this matters to you.
- No command-line arguments; thread count and MAX_CRAWL must be changed
  by editing the source.
- Per-domain politeness is not enforced with an explicit delay; it
  relies on the priority formula naturally discouraging repeated
  back-to-back requests to the same domain. See explain.txt for more
  detail on this design choice and its limits.
- Depth recorded for a URL reflects whichever push happened to be popped
  and crawled first, not necessarily the shortest path from a seed page.
