# CORE Econ Book Crawler

Crawls [books.core-econ.org](https://books.core-econ.org) and extracts chapter
content as clean **Markdown** or **PDF** files — suitable for importing into
NotebookLM or other LLM tools.

## Quick start

```bash
pip install -r requirements.txt

# Markdown (text only, lightweight)
python crawler.py

# PDF (preserves images, figures, and formatting)
python crawler.py --format pdf
```

Output lands in `./output/` — one numbered file per section.

## Options

```
--format FORMAT     markdown or pdf (default: markdown)
--url URL           Table-of-contents page to crawl (default: Microeconomics)
--output-dir DIR    Where to save files (default: output/)
--single-file       Also produce one merged file (book-complete.md or .pdf)
--delay SECONDS     Wait between requests (default: 1.0)
--verbose           Enable debug logging
```

### Examples

Crawl as Markdown (text only):

```bash
python crawler.py
```

Crawl as PDF with images, merged into a single file for NotebookLM:

```bash
python crawler.py --format pdf --single-file
```

Crawl a different book (e.g. Macroeconomics):

```bash
python crawler.py \
  --format pdf \
  --url "https://books.core-econ.org/the-economy/macroeconomics/0-3-contents.html" \
  --output-dir output-macro \
  --single-file
```

## How it works

1. Fetches the table-of-contents page
2. Discovers all section links
3. Crawls each section page with polite delays
4. Strips navigation chrome (nav, header, footer, menus)
5. **Markdown mode** — converts to clean text via `html2text`
6. **PDF mode** — renders to PDF via `weasyprint`, preserving images and layout
7. Saves numbered files, optionally merges into one

## Choosing a format

| | Markdown | PDF |
|---|---|---|
| Images & figures | stripped | preserved |
| File size | small | larger |
| Best for | text-focused LLMs | NotebookLM, visual reference |
| Dependencies | lightweight | needs `weasyprint` system libs |

## System dependencies for PDF

`weasyprint` requires a few system libraries. On most systems:

```bash
# Ubuntu / Debian
sudo apt install libpango-1.0-0 libpangocairo-1.0-0 libgdk-pixbuf2.0-0

# macOS (Homebrew)
brew install pango libffi

# For full details see https://doc.courtbouillon.org/weasyprint/stable/first_steps.html
```
