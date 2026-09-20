#!/usr/bin/env python3
"""Index public TOPIK source links and optionally save PDF bytes locally.

No uploads, Git commands, or automatic licensing assumptions are performed.
Only the prelisted landing pages are scanned, and only PDF-validated files are saved.
"""
from __future__ import annotations
import argparse
import csv
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import sys
import time
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

USER_AGENT = 'TopikDatabase-research/0.1 (manual personal source index)'
HEADERS = {'User-Agent': USER_AGENT, 'Accept': 'text/html,application/pdf;q=0.9,*/*;q=0.5'}
MAX_PDF_BYTES = 40 * 1024 * 1024
CATEGORIES = ('I', 'II')


def norm(value: str) -> str:
    return re.sub(r'\s+', ' ', value).strip()


def level_from(text: str) -> str | None:
    t = norm(text).upper().replace('TOPIKⅠ', 'TOPIK I').replace('TOPIKⅡ', 'TOPIK II')
    match = re.search(r'\bTOPIK\s*(II|I|2|1)\b', t)
    if not match:
        return None
    return 'II' if match.group(1) in ('II', '2') else 'I'


def file_category(text: str) -> tuple[str, str]:
    """Return (kind, section), preserving uncertainty as UNKNOWN."""
    t = text.upper()
    if re.search(r'ANSWER|정답', t):
        kind = 'ANSWER_KEY'
    elif re.search(r'TRANSCRIPT|LISTENING TEXT|듣기\s*대본|듣기\s*텍스트', t):
        kind = 'TRANSCRIPT'
    elif re.search(r'TEST PAPER|QUESTION|문제지|기출\s*문제', t):
        kind = 'QUESTIONS'
    else:
        kind = 'UNCLASSIFIED'
    sections = [name for name, rx in [('READING', r'READING|읽기'), ('LISTENING', r'LISTENING|듣기'), ('WRITING', r'WRITING|쓰기')] if re.search(rx, t)]
    return kind, '_'.join(sections) if sections else 'COMBINED_OR_UNKNOWN'


class LinkParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.heading = None
        self.heading_pieces = []
        self.current_level = None
        self.anchor_href = None
        self.anchor_pieces = []
        self.items = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in ('h2', 'h3', 'h4'):
            self.heading = tag
            self.heading_pieces = []
        elif tag == 'a' and attrs.get('href') and self.anchor_href is None:
            self.anchor_href = attrs['href']
            self.anchor_pieces = []

    def handle_data(self, data):
        if self.heading:
            self.heading_pieces.append(data)
        if self.anchor_href is not None:
            self.anchor_pieces.append(data)

    def handle_endtag(self, tag):
        if tag == self.heading:
            inferred = level_from(''.join(self.heading_pieces))
            if inferred:
                self.current_level = inferred
            self.heading = None
            self.heading_pieces = []
        elif tag == 'a' and self.anchor_href is not None:
            self.items.append((self.anchor_href, norm(''.join(self.anchor_pieces)), self.current_level))
            self.anchor_href = None
            self.anchor_pieces = []


def is_pdf_candidate(href: str, label: str) -> bool:
    path = urlparse(href).path.lower()
    if path.endswith('.pdf'):
        return True
    # Some source pages use redirects without a .pdf suffix.
    if re.search(r'TEST PAPER|ANSWER KEY|LISTENING TEXT|TRANSCRIPT|문제지|정답', label, re.I):
        return True
    return False


def read_bytes(url: str, max_bytes: int) -> bytes:
    req = Request(url, headers=HEADERS)
    with urlopen(req, timeout=35) as response:
        size = response.headers.get('Content-Length')
        if size and int(size) > max_bytes:
            raise ValueError(f'remote file exceeds {max_bytes} bytes')
        data = response.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError(f'download exceeds {max_bytes} bytes')
    return data


def read_sources(path: Path):
    with path.open(encoding='utf-8-sig', newline='') as f:
        for row in csv.DictReader(f):
            yield int(row['session']), row['source_page']


def inspect_page(session: int, page: str) -> list[dict]:
    markup = read_bytes(page, 6 * 1024 * 1024).decode('utf-8', errors='replace')
    parser = LinkParser()
    parser.feed(markup)
    found = []
    for href, label, heading_level in parser.items:
        if not is_pdf_candidate(href, label):
            continue
        url = urljoin(page, href)
        if urlparse(url).scheme not in ('https', 'http'):
            continue
        level = level_from(label) or level_from(href) or heading_level
        # Unknown level is deliberately retained in the inventory, but not silently renamed as I/II.
        kind, section = file_category(label + ' ' + href)
        found.append({'session': session, 'level': level or 'UNKNOWN', 'kind': kind,
                      'section': section, 'label': label, 'url': url, 'source_page': page,
                      'status': 'indexed', 'file': '', 'sha256': '', 'error': ''})
    deduped = {row['url']: row for row in found}
    return list(deduped.values())


def unique_filename(row: dict, n: int) -> str:
    return f"TOPIK_{row['session']:03d}_{row['level']}_{row['section']}_{row['kind']}_{n:02d}.pdf"


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--root', type=Path, default=Path(__file__).resolve().parent, help='Project directory')
    ap.add_argument('--download', action='store_true', help='Save matching PDFs locally after validating %%PDF-')
    ap.add_argument('--limit', type=int, default=0, help='Optional maximum number of files to download; 0 means all')
    args = ap.parse_args()
    root = args.root.resolve()
    sources = root / 'source_pages.csv'
    if not sources.exists():
        ap.error(f'Could not locate {sources}')
    folder = root / 'local_sources'
    inventory_dir = root / 'catalog'
    inventory_dir.mkdir(parents=True, exist_ok=True)
    records = []
    attempted = 0
    for session, page in read_sources(sources):
        print(f'[scan] {session:03d}: {page}', flush=True)
        try:
            links = inspect_page(session, page)
        except Exception as exc:
            records.append({'session': session, 'level': 'UNKNOWN', 'kind': 'UNCLASSIFIED',
                            'section': 'COMBINED_OR_UNKNOWN', 'label': '', 'url': '',
                            'source_page': page, 'status': 'page_error', 'file': '',
                            'sha256': '', 'error': f'{type(exc).__name__}: {exc}'})
            continue
        print(f'       indexed {len(links)} PDF candidates')
        for index, row in enumerate(links, 1):
            if not args.download:
                records.append(row)
                continue
            if args.limit and attempted >= args.limit:
                row['status'] = 'skipped_limit'
                records.append(row)
                continue
            attempted += 1
            category = row['level'] if row['level'] in CATEGORIES else 'UNKNOWN'
            destination = folder / f"{session:03d}" / f'TOPIK_{category}' / unique_filename(row, index)
            try:
                if destination.exists():
                    existing = destination.read_bytes()
                    if not existing.startswith(b'%PDF-'):
                        raise ValueError('existing path is not a PDF: refusing to overwrite')
                    data = existing
                    row['status'] = 'already_present'
                else:
                    data = read_bytes(row['url'], MAX_PDF_BYTES)
                    if not data.startswith(b'%PDF-') or b'%%EOF' not in data[-2048:]:
                        raise ValueError('response lacks PDF signature/trailer (possibly HTML or broken PDF)')
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(data)
                    row['status'] = 'saved'
                row['file'] = str(destination.relative_to(root)).replace('\\', '/')
                row['sha256'] = hashlib.sha256(data).hexdigest()
            except Exception as exc:
                row['status'] = 'file_error'
                row['error'] = f'{type(exc).__name__}: {exc}'
            records.append(row)
            time.sleep(0.65)
    csv_path = inventory_dir / 'pdf_inventory.csv'
    keys = ['session', 'level', 'kind', 'section', 'label', 'url', 'source_page', 'status', 'file', 'sha256', 'error']
    with csv_path.open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=keys)
        writer.writeheader()
        writer.writerows(records)
    (inventory_dir / 'collection_summary.json').write_text(json.dumps({
        'source_count': sum(1 for _ in read_sources(sources)), 'candidate_count': sum(bool(r['url']) for r in records),
        'saved': sum(r['status'] == 'saved' for r in records),
        'already_present': sum(r['status'] == 'already_present' for r in records),
        'error_count': sum(r['status'].endswith('error') for r in records),
        'pdfs_uploaded_to_github': 0,
    }, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(f'Inventory: {csv_path}')
    print(f"PDFs saved this run: {sum(r['status'] == 'saved' for r in records)} / candidates {sum(bool(r['url']) for r in records)}")
    print('No Git operation performed; original PDFs are ignored by .gitignore.')
    return 0 if not any(r['status'] == 'page_error' for r in records) else 2


if __name__ == '__main__':
    sys.exit(main())
