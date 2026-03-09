"""
CORE Econ Book Crawler
======================
Crawls books.core-econ.org and extracts chapter content as clean Markdown
or PDF files suitable for importing into NotebookLM or other LLM tools.

Usage:
    python crawler.py                                # markdown (default)
    python crawler.py --format pdf                   # PDF with images
    python crawler.py --format pdf --single-file     # one merged PDF
    python crawler.py --url URL                      # crawl a custom book
    python crawler.py --output-dir my_output         # specify output dir
    python crawler.py --delay 2.0                    # seconds between requests
"""

import argparse
import base64
import io
import logging
import mimetypes
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

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

    seen: set[str] = set()
    links: list[dict[str, str]] = []

    content_area = (
        soup.select_one("main")
        or soup.select_one("#content")
        or soup.select_one(".content")
        or soup.select_one("article")
        or soup.body
    )

    for a_tag in content_area.find_all("a", href=True):
        href: str = a_tag["href"]

        if href.startswith("#") or href.startswith("mailto:"):
            continue

        absolute = urljoin(contents_url, href).split("#")[0]

        if not absolute.startswith(base_url):
            continue
        if not absolute.endswith(".html"):
            continue
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
# HTML cleaning (shared by both exporters)
# ---------------------------------------------------------------------------
NOISE_SELECTORS = (
    "nav, header, footer, script, style, "
    ".sidebar, .navigation, .nav, .menu, .breadcrumb, "
    ".cookie-banner, .share-buttons, #cookie-notice"
)
# NOTE: <noscript> is intentionally NOT stripped — lazy-loading frameworks
# (like the Electric Book toolkit used by core-econ.org) place fallback
# <img> tags inside <noscript> elements.


def _download_as_data_uri(session: requests.Session, url: str) -> str | None:
    """Download a resource and return it as a base64 data URI."""
    try:
        resp = session.get(url, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.debug("Failed to download image %s: %s", url, exc)
        return None

    content_type = resp.headers.get("Content-Type", "").split(";")[0].strip()
    if not content_type:
        content_type = mimetypes.guess_type(url)[0] or "application/octet-stream"

    b64 = base64.b64encode(resp.content).decode("ascii")
    return f"data:{content_type};base64,{b64}"


def clean_html(
    html: str, page_url: str, session: requests.Session | None = None
) -> BeautifulSoup:
    """
    Parse HTML, strip navigation chrome, and resolve relative URLs so
    images and links work in both Markdown and PDF output.

    When *session* is provided, images are downloaded using our requests
    session and embedded as base64 data URIs.  This guarantees images
    appear in PDFs — WeasyPrint's own fetcher often gets blocked by
    servers that reject its User-Agent or require cookies.
    """
    soup = BeautifulSoup(html, "html.parser")

    for tag in soup.select(NOISE_SELECTORS):
        tag.decompose()

    # Unwrap <noscript> tags so their content (often lazy-load fallback
    # images) becomes part of the visible DOM.
    for noscript in soup.find_all("noscript"):
        noscript.unwrap()

    # Promote data-src / data-lazy-src to src for lazy-loaded images
    for img in soup.find_all("img"):
        if not img.get("src") or img["src"].startswith("data:image/gif"):
            for attr in ("data-src", "data-lazy-src", "data-original"):
                if img.get(attr):
                    img["src"] = img[attr]
                    break

    # Process images: resolve URLs and optionally embed as data URIs
    for img in soup.find_all("img", src=True):
        absolute_src = urljoin(page_url, img["src"])

        if session is not None:
            data_uri = _download_as_data_uri(session, absolute_src)
            if data_uri:
                img["src"] = data_uri
                # srcset is irrelevant once we've embedded the image
                if img.get("srcset"):
                    del img["srcset"]
                continue

        # Fallback: just resolve to absolute URL
        img["src"] = absolute_src

    # Resolve remaining srcset entries (only when images weren't embedded)
    for img in soup.find_all("img", srcset=True):
        parts = []
        for entry in img["srcset"].split(","):
            entry = entry.strip()
            if not entry:
                continue
            tokens = entry.split()
            tokens[0] = urljoin(page_url, tokens[0])
            parts.append(" ".join(tokens))
        img["srcset"] = ", ".join(parts)

    # Resolve relative link hrefs
    for a_tag in soup.find_all("a", href=True):
        a_tag["href"] = urljoin(page_url, a_tag["href"])

    # Resolve <link> stylesheet hrefs for PDF rendering
    for link in soup.find_all("link", href=True):
        link["href"] = urljoin(page_url, link["href"])

    return soup


def find_main_content(soup: BeautifulSoup):
    """Return the main content element from a cleaned soup."""
    return (
        soup.select_one("main")
        or soup.select_one("article")
        or soup.select_one("#content")
        or soup.select_one(".content")
        or soup.select_one('[role="main"]')
        or soup.body
    )


# ---------------------------------------------------------------------------
# Markdown exporter
# ---------------------------------------------------------------------------
def _build_html2text():
    import html2text

    h = html2text.HTML2Text()
    h.body_width = 0
    h.ignore_links = False
    h.ignore_images = True
    h.ignore_emphasis = False
    h.skip_internal_links = True
    h.inline_links = False
    h.protect_links = True
    h.wrap_links = False
    return h


def extract_markdown(html: str, url: str) -> str:
    """Extract main content as clean Markdown."""
    soup = clean_html(html, url)
    main = find_main_content(soup)
    if main is None:
        return ""

    converter = _build_html2text()
    markdown = converter.handle(str(main))
    markdown = re.sub(r"\n{4,}", "\n\n\n", markdown)
    return markdown.strip()


# ---------------------------------------------------------------------------
# PDF exporter
# ---------------------------------------------------------------------------
def extract_pdf_bytes(
    html: str, page_url: str, session: requests.Session | None = None
) -> bytes | None:
    """
    Render a section page to a PDF byte string using WeasyPrint.
    Images are pre-downloaded via our requests session and embedded as
    base64 data URIs so they always appear in the output PDF.
    """
    # Silence noisy WeasyPrint / fontTools subsetting logs
    for noisy in ("weasyprint", "fontTools", "fontTools.subset"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    from weasyprint import HTML as WeasyHTML

    soup = clean_html(html, page_url, session=session)

    # Build a self-contained HTML document for WeasyPrint.
    # Keep the original <head> (stylesheets, meta charset) but inject a
    # base tag so relative resources resolve correctly.
    head = soup.find("head")
    if head is None:
        head_html = ""
    else:
        # Remove any existing <base> to avoid conflicts
        for base in head.find_all("base"):
            base.decompose()
        head_html = str(head)

    main = find_main_content(soup)
    if main is None:
        return None

    # Inject a small print-friendly stylesheet
    print_css = """
    <style>
        body { font-family: serif; line-height: 1.6; margin: 2cm; color: #222; }
        img { max-width: 100%; height: auto; }
        table { border-collapse: collapse; width: 100%; margin: 1em 0; }
        td, th { border: 1px solid #ccc; padding: 0.4em 0.6em; }
        h1, h2, h3 { margin-top: 1.2em; }
        figure { margin: 1em 0; text-align: center; }
        figcaption { font-size: 0.9em; color: #555; }
        @page { margin: 2cm; }
    </style>
    """

    doc_html = (
        "<!DOCTYPE html>\n<html>\n"
        f"<head>\n<base href=\"{page_url}\">\n"
        f"<meta charset=\"utf-8\">\n{print_css}\n"
        f"</head>\n<body>\n{main}\n</body>\n</html>"
    )

    try:
        pdf_bytes = WeasyHTML(string=doc_html, base_url=page_url).write_pdf()
        return pdf_bytes
    except Exception as exc:
        log.error("PDF rendering failed for %s: %s", page_url, exc)
        return None


def merge_pdfs(pdf_list: list[bytes]) -> bytes:
    """Merge multiple PDF byte strings into one."""
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter()
    for pdf_bytes in pdf_list:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        for page in reader.pages:
            writer.add_page(page)

    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Filename / save helpers
# ---------------------------------------------------------------------------
def sanitize_filename(name: str) -> str:
    """Turn a section title into a safe filename."""
    name = re.sub(r"[^\w\s\-.]", "", name)
    name = re.sub(r"\s+", "-", name.strip())
    return name[:120] or "untitled"


def save_markdown_section(
    output_dir: Path, index: int, title: str, url: str, content: str
) -> Path:
    filename = f"{index:03d}-{sanitize_filename(title)}.md"
    path = output_dir / filename
    header = f"# {title}\n\nSource: {url}\n\n---\n\n"
    path.write_text(header + content, encoding="utf-8")
    return path


def save_merged_markdown(output_dir: Path, sections: list[dict]) -> Path:
    path = output_dir / "book-complete.md"
    parts: list[str] = []
    for sec in sections:
        parts.append(f"# {sec['title']}\n\nSource: {sec['url']}\n\n---\n\n")
        parts.append(sec["content"])
        parts.append("\n\n---\n\n")
    path.write_text("".join(parts), encoding="utf-8")
    return path


def save_pdf_section(
    output_dir: Path, index: int, title: str, pdf_bytes: bytes
) -> Path:
    filename = f"{index:03d}-{sanitize_filename(title)}.pdf"
    path = output_dir / filename
    path.write_bytes(pdf_bytes)
    return path


def save_merged_pdf(output_dir: Path, pdf_list: list[bytes]) -> Path:
    path = output_dir / "book-complete.pdf"
    merged = merge_pdfs(pdf_list)
    path.write_bytes(merged)
    return path


# ---------------------------------------------------------------------------
# Main crawl loop
# ---------------------------------------------------------------------------
def crawl(
    contents_url: str,
    output_dir: str,
    fmt: str,
    delay: float,
    single_file: bool,
) -> None:
    session = make_session()
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    log.info("Contents URL : %s", contents_url)
    log.info("Output dir   : %s", out.resolve())
    log.info("Format       : %s", fmt)
    log.info("Request delay: %.1fs", delay)

    # Step 1 – discover links
    links = discover_section_links(session, contents_url)
    if not links:
        log.error("No section links found – check the contents URL")
        sys.exit(1)

    # Step 2 – crawl each section
    count = 0
    md_sections: list[dict] = []  # for markdown --single-file
    pdf_parts: list[bytes] = []   # for pdf --single-file

    for i, link in enumerate(links, start=1):
        url = link["url"]
        title = link["title"]
        log.info("[%d/%d] Fetching: %s", i, len(links), title)

        html = fetch(session, url)
        if html is None:
            log.warning("Skipping %s (fetch failed)", url)
            continue

        if fmt == "markdown":
            content = extract_markdown(html, url)
            if not content:
                log.warning("Skipping %s (no content extracted)", url)
                continue
            path = save_markdown_section(out, i, title, url, content)
            log.info("  -> saved %s (%d chars)", path.name, len(content))
            md_sections.append({"title": title, "url": url, "content": content})

        else:  # pdf
            pdf_bytes = extract_pdf_bytes(html, url, session=session)
            if pdf_bytes is None:
                log.warning("Skipping %s (PDF rendering failed)", url)
                continue
            path = save_pdf_section(out, i, title, pdf_bytes)
            log.info("  -> saved %s (%.1f KB)", path.name, len(pdf_bytes) / 1024)
            pdf_parts.append(pdf_bytes)

        count += 1
        if i < len(links):
            time.sleep(delay)

    # Step 3 – optionally merge
    if single_file:
        if fmt == "markdown" and md_sections:
            merged = save_merged_markdown(out, md_sections)
            log.info("Merged file: %s", merged)
        elif fmt == "pdf" and pdf_parts:
            merged = save_merged_pdf(out, pdf_parts)
            log.info("Merged file: %s (%.1f MB)", merged, merged.stat().st_size / 1e6)

    log.info("Done – %s %d/%d sections into %s", fmt, count, len(links), out.resolve())


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Crawl a CORE Econ book and export as Markdown or PDF.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--url",
        default=DEFAULT_CONTENTS_URL,
        help="Table-of-contents page URL (default: CORE Econ Microeconomics)",
    )
    parser.add_argument(
        "--format",
        choices=["markdown", "pdf"],
        default="markdown",
        dest="fmt",
        help="Output format (default: markdown)",
    )
    parser.add_argument(
        "--output-dir",
        default=DEFAULT_OUTPUT_DIR,
        help="Directory to save output files (default: %(default)s)",
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
        help="Also produce a single merged file (book-complete.md or .pdf)",
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
        fmt=args.fmt,
        delay=args.delay,
        single_file=args.single_file,
    )


if __name__ == "__main__":
    main()
