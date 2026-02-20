# CORE Econ Book Crawler

Crawls [books.core-econ.org](https://books.core-econ.org) and extracts chapter
text as clean Markdown files suitable for importing into NotebookLM or other LLM
tools.

## Quick start

```bash
pip install -r requirements.txt
python crawler.py
```

Output lands in `./output/` — one numbered `.md` file per section.

## Options

```
--url URL           Table-of-contents page to crawl (default: Microeconomics)
--output-dir DIR    Where to save files (default: output/)
--single-file       Also produce one merged book-complete.md
--delay SECONDS     Wait between requests (default: 1.0)
--verbose           Enable debug logging
```

### Examples

Crawl the default Microeconomics book:

```bash
python crawler.py
```

Crawl a different book (e.g. Macroeconomics) and merge into one file:

```bash
python crawler.py \
  --url "https://books.core-econ.org/the-economy/macroeconomics/0-3-contents.html" \
  --output-dir output-macro \
  --single-file
```

## How it works

1. Fetches the table-of-contents page
2. Discovers all section links
3. Crawls each section page with polite delays
4. Strips navigation/chrome and converts to clean Markdown
5. Saves numbered files (and optionally a merged file)

## For NotebookLM

- Use `--single-file` to get one `book-complete.md` you can upload directly
- Or upload individual chapter files for more granular source references
