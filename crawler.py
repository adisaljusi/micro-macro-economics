"""
CORE Econ Book Crawler
======================
Crawls books.core-econ.org and extracts chapter text as clean Markdown
files suitable for importing into NotebookLM or other LLM tools.

Usage:
    python crawler.py                          # crawl default (microeconomics)
    python crawler.py --url URL                # crawl a custom contents page
    python crawler.py --output-dir my_output   # specify output directory
    python crawler.py --single-file            # merge all chapters into one file
    python crawler.py --delay 2.0              # seconds between requests
"""

import argparse
import logging
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import html2text
import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
DEFAULT_CONTENTS_URL = (
    "https://books.core-econ.org/the-economy/microeconomics/0-3-contents.html"
)
DEFAULT_OUTPUT_DIR = "output"
DEFAULT_DELAY = 1.0  # polite delay between requests (seconds)
REQUEST_TIMEOUT = 30  # seconds
MAX_RETRIES = 3

USER_AGENT = (
    "CoreEconBookCrawler/1.0 (educational-use; "
    "https://github.com/core-econ-crawler)"
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------
def make_session() -> requests.Session:
    """Create a requests session with sensible defaults."""
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.9",
        }
    )
    return session


def fetch(session: requests.Session, url: str) -> str | None:
    """Fetch a URL with retries and exponential back-off."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.get(url, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as exc:
            wait = 2**attempt
            log.warning(
                "Attempt %d/%d failed for %s: %s – retrying in %ds",
                attempt,
                MAX_RETRIES,
                url,
                exc,
                wait,
            )
            time.sleep(wait)
    log.error("Failed to fetch %s after %d attempts", url, MAX_RETRIES)
    return None


# ---------------------------------------------------------------------------
# Link discovery
# ---------------------------------------------------------------------------
def discover_section_links(
    session: requests.Session, contents_url: str
) -> list[dict[str, str]]:
    """
    Parse the table-of-contents page and return an ordered list of
    ``{"url": ..., "title": ...}`` dicts for every section link.
    """
    html = fetch(session, contents_url)
    if html is None:
        log.error("Could not fetch contents page – aborting discovery")
        sys.exit(1)

    soup = BeautifulSoup(html, "html.parser")
    base_url = contents_url.rsplit("/", 1)[0] + "/"

    # The contents page links to individual sections via <a> tags.
    # We collect all internal links that point to .html pages under the same
    # base path, excluding anchors to the same page and non-chapter assets.
    seen: set[str] = set()
    links: list[dict[str, str]] = []

    # Identify the main content area (try common selectors)
    content_area = (
        soup.select_one("main")
        or soup.select_one("#content")
        or soup.select_one(".content")
        or soup.select_one("article")
        or soup.body
    )

    for a_tag in content_area.find_all("a", href=True):
        href: str = a_tag["href"]

        # Skip pure anchors, external links, and non-html resources
        if href.startswith("#") or href.startswith("mailto:"):
            continue

        absolute = urljoin(contents_url, href).split("#")[0]

        # Must stay within the same book directory
        if not absolute.startswith(base_url):
            continue
        if not absolute.endswith(".html"):
            continue
        # Skip the contents page itself
        if absolute == contents_url:
            continue

        if absolute in seen:
            continue
        seen.add(absolute)

        title = a_tag.get_text(strip=True) or Path(urlparse(absolute).path).stem
        links.append({"url": absolute, "title": title})

    log.info("Discovered %d section links from %s", len(links), contents_url)
    return links


# ---------------------------------------------------------------------------
# Content extraction
# ---------------------------------------------------------------------------
def _build_html2text() -> html2text.HTML2Text:
    """Configure html2text for clean Markdown output."""
    h = html2text.HTML2Text()
    h.body_width = 0  # don't wrap lines
    h.ignore_links = False
    h.ignore_images = True
    h.ignore_emphasis = False
    h.skip_internal_links = True
    h.inline_links = False  # use reference-style links
    h.protect_links = True
    h.wrap_links = False
    return h


def extract_page_content(html: str, url: str) -> str:
    """
    Extract the main textual content from a section page and return
    clean Markdown.
    """
    soup = BeautifulSoup(html, "html.parser")

    # Remove elements that add noise for LLM ingestion
    for tag in soup.select(
        "nav, header, footer, script, style, noscript, "
        ".sidebar, .navigation, .nav, .menu, .breadcrumb, "
        ".cookie-banner, .share-buttons, #cookie-notice"
    ):
        tag.decompose()

    # Find the main content container
    main = (
        soup.select_one("main")
        or soup.select_one("article")
        or soup.select_one("#content")
        or soup.select_one(".content")
        or soup.select_one('[role="main"]')
        or soup.body
    )

    if main is None:
        return ""

    converter = _build_html2text()
    markdown = converter.handle(str(main))

    # Light clean-up: collapse excessive blank lines
    markdown = re.sub(r"\n{4,}", "\n\n\n", markdown)
    return markdown.strip()


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------
def sanitize_filename(name: str) -> str:
    """Turn a section title into a safe filename."""
    name = re.sub(r"[^\w\s\-.]", "", name)
    name = re.sub(r"\s+", "-", name.strip())
    return name[:120] or "untitled"


def save_section(
    output_dir: Path, index: int, title: str, url: str, content: str
) -> Path:
    """Save a single section as a numbered Markdown file."""
    filename = f"{index:03d}-{sanitize_filename(title)}.md"
    path = output_dir / filename
    header = f"# {title}\n\nSource: {url}\n\n---\n\n"
    path.write_text(header + content, encoding="utf-8")
    return path


def save_merged(output_dir: Path, sections: list[dict]) -> Path:
    """Merge all sections into a single Markdown file."""
    path = output_dir / "book-complete.md"
    parts: list[str] = []
    for sec in sections:
        parts.append(f"# {sec['title']}\n\nSource: {sec['url']}\n\n---\n\n")
        parts.append(sec["content"])
        parts.append("\n\n---\n\n")
    path.write_text("".join(parts), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Main crawl loop
# ---------------------------------------------------------------------------
def crawl(
    contents_url: str,
    output_dir: str,
    delay: float,
    single_file: bool,
) -> None:
    session = make_session()
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    log.info("Contents URL : %s", contents_url)
    log.info("Output dir   : %s", out.resolve())
    log.info("Request delay: %.1fs", delay)

    # Step 1 – discover links
    links = discover_section_links(session, contents_url)
    if not links:
        log.error("No section links found – check the contents URL")
        sys.exit(1)

    # Step 2 – crawl each section
    collected: list[dict] = []
    for i, link in enumerate(links, start=1):
        url = link["url"]
        title = link["title"]
        log.info("[%d/%d] Fetching: %s", i, len(links), title)

        html = fetch(session, url)
        if html is None:
            log.warning("Skipping %s (fetch failed)", url)
            continue

        content = extract_page_content(html, url)
        if not content:
            log.warning("Skipping %s (no content extracted)", url)
            continue

        # Save individual file
        path = save_section(out, i, title, url, content)
        log.info("  -> saved %s (%d chars)", path.name, len(content))

        collected.append({"title": title, "url": url, "content": content})

        if i < len(links):
            time.sleep(delay)

    # Step 3 – optionally merge into one file
    if single_file and collected:
        merged = save_merged(out, collected)
        log.info("Merged file: %s", merged)

    log.info(
        "Done – crawled %d/%d sections into %s",
        len(collected),
        len(links),
        out.resolve(),
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Crawl a CORE Econ book and export clean Markdown for LLM use.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--url",
        default=DEFAULT_CONTENTS_URL,
        help="Table-of-contents page URL (default: CORE Econ Microeconomics)",
    )
    parser.add_argument(
        "--output-dir",
        default=DEFAULT_OUTPUT_DIR,
        help="Directory to save extracted Markdown files (default: %(default)s)",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=DEFAULT_DELAY,
        help="Seconds to wait between requests (default: %(default)s)",
    )
    parser.add_argument(
        "--single-file",
        action="store_true",
        help="Also produce a single merged Markdown file",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug logging",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    crawl(
        contents_url=args.url,
        output_dir=args.output_dir,
        delay=args.delay,
        single_file=args.single_file,
    )


if __name__ == "__main__":
    main()
