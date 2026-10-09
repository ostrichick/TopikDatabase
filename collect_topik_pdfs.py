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
import io
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from urllib.parse import urljoin, urlparse, urlsplit, urlunsplit
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
    if size is not None and len(data) != int(size):
        raise ValueError(f'incomplete download: Content-Length {size}, received {len(data)} bytes')
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


def canonical_url(url: str) -> str:
    """Keep resource-specific path/query, normalize origin and drop non-resource fragments."""
    parts = urlsplit(url.strip())
    if parts.scheme.lower() not in ('http', 'https') or not parts.hostname or parts.username is not None:
        raise ValueError('invalid public PDF URL')
    scheme = parts.scheme.lower()
    host = parts.hostname.lower()
    if ':' in host:
        host = f'[{host}]'
    port = parts.port
    netloc = host if port is None or (scheme, port) in (('http', 80), ('https', 443)) else f'{host}:{port}'
    return urlunsplit((scheme, netloc, parts.path or '/', parts.query, ''))


def provenance_key(row: dict) -> str:
    return f"{int(row['session']):03d}:{canonical_url(row['url'])}"


def unique_filename(row: dict, n: int | None = None) -> str:
    """URL-derived name independent of link order; n is ignored for caller compatibility."""
    url_hash = hashlib.sha256(canonical_url(row['url']).encode('utf-8')).hexdigest()[:24]
    parts = (f"{int(row['session']):03d}", row['level'], row['section'], row['kind'])
    safe_parts = [re.sub(r'[^A-Za-z0-9_]', '_', str(part)) for part in parts]
    return f"TOPIK_{'_'.join(safe_parts)}_{url_hash}.pdf"


def validate_pdf(data: bytes) -> str:
    """Reject obviously incomplete/corrupt downloads before saving or reusing."""
    if not 32 <= len(data) <= MAX_PDF_BYTES:
        raise ValueError(f'PDF has invalid size: {len(data)} bytes')
    if not re.match(rb'%PDF-[12]\.\d', data) or not re.search(rb'%%EOF\s*\Z', data):
        raise ValueError('PDF lacks a valid header or final %%EOF trailer')
    return hashlib.sha256(data).hexdigest()


def _atomic_write(path: Path, content: bytes) -> None:
    """Replace a catalog record atomically without leaving a partially written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f'.{path.name}.', suffix='.tmp',
                                         delete=False) as output:
            temp_path = Path(output.name)
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def _atomic_save_pdf(path: Path, content: bytes) -> None:
    """Atomically publish a new PDF and never replace an existing path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.partial-', suffix='.tmp',
                                         delete=False) as output:
            temp_path = Path(output.name)
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        # An atomic hard link fails if another process already created the destination.
        os.link(temp_path, path)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def load_provenance(path: Path) -> dict:
    """Read only schema-validated mappings; never guess provenance from old filenames."""
    if not path.exists():
        return {}
    document = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(document, dict) or document.get('version') != 1 or not isinstance(document.get('records'), dict):
        raise ValueError('invalid PDF provenance manifest schema')
    seen_files = {}
    for key, record in document['records'].items():
        if not isinstance(record, dict) or set(record) != {'session', 'url', 'file', 'sha256'}:
            raise ValueError('invalid PDF provenance record')
        if not isinstance(record['session'], int) or not isinstance(record['url'], str) or not isinstance(record['file'], str):
            raise ValueError('invalid PDF provenance identity')
        if provenance_key(record) != key or canonical_url(record['url']) != record['url']:
            raise ValueError('PDF provenance URL/key mismatch')
        file = Path(record['file'])
        if file.is_absolute() or '..' in file.parts or not file.parts or file.parts[0] != 'local_sources':
            raise ValueError('unsafe PDF provenance path')
        if not isinstance(record['sha256'], str) or not re.fullmatch(r'[0-9a-f]{64}', record['sha256']):
            raise ValueError('invalid PDF provenance sha256')
        if record['file'] in seen_files and seen_files[record['file']] != key:
            raise ValueError('two PDF URLs share a provenance path')
        seen_files[record['file']] = key
    return document['records']


def validate_previous_inventory(path: Path, manifest: dict) -> None:
    """Check provenance-bearing inventory rows; do not trust sequential legacy rows."""
    if not path.exists():
        return
    with path.open(encoding='utf-8-sig', newline='') as stream:
        for row in csv.DictReader(stream):
            stored_file = row.get('file', '')
            if not re.search(r'_[0-9a-f]{24}\.pdf\Z', stored_file):
                continue  # Historical numbered filenames cannot establish URL provenance.
            key = provenance_key(row)
            mapping = manifest.get(key)
            if mapping is None or mapping['file'] != stored_file or mapping['sha256'] != row['sha256']:
                raise ValueError('previous PDF inventory conflicts with provenance manifest')


def download_or_reuse(row: dict, root: Path, folder: Path, manifest: dict, manifest_path: Path) -> None:
    category = row['level'] if row['level'] in CATEGORIES else 'UNKNOWN'
    destination = folder / f"{int(row['session']):03d}" / f'TOPIK_{category}' / unique_filename(row)
    relative = destination.relative_to(root).as_posix()
    key = provenance_key(row)
    expected = manifest.get(key)
    if expected is not None and (expected['file'] != relative or expected['url'] != canonical_url(row['url'])):
        raise ValueError('PDF provenance conflict: URL was previously assigned another file')
    if any(other_key != key and entry['file'] == relative for other_key, entry in manifest.items()):
        raise ValueError('PDF provenance conflict: filename already assigned to another URL')

    if destination.exists():
        if expected is None:
            raise ValueError('existing PDF has no verified provenance: refusing to reuse or overwrite')
        digest = validate_pdf(destination.read_bytes())
        if digest != expected['sha256']:
            raise ValueError('existing PDF SHA-256 differs from recorded provenance')
        row['status'] = 'already_present'
    else:
        data = read_bytes(row['url'], MAX_PDF_BYTES)
        digest = validate_pdf(data)
        if expected is not None and digest != expected['sha256']:
            raise ValueError('download SHA-256 conflicts with recorded PDF provenance')
        _atomic_save_pdf(destination, data)
        if expected is None:
            manifest[key] = {'session': int(row['session']), 'url': canonical_url(row['url']),
                             'file': relative, 'sha256': digest}
            try:
                _atomic_write(manifest_path, (json.dumps({'version': 1, 'records': manifest},
                                                         ensure_ascii=False, indent=2) + '\n').encode('utf-8'))
            except Exception:
                manifest.pop(key, None)
                raise
        row['status'] = 'saved'
    row['file'] = relative
    row['sha256'] = digest


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--root', type=Path, default=Path(__file__).resolve().parent, help='Project directory')
    ap.add_argument('--download', action='store_true', help='Save only validated PDFs with recorded URL/SHA-256 provenance')
    ap.add_argument('--limit', type=int, default=0, help='Optional maximum number of files to download; 0 means all')
    args = ap.parse_args()
    root = args.root.resolve()
    sources = root / 'source_pages.csv'
    if not sources.exists():
        ap.error(f'Could not locate {sources}')
    folder = root / 'local_sources'
    inventory_dir = root / 'catalog'
    inventory_dir.mkdir(parents=True, exist_ok=True)
    csv_path = inventory_dir / 'pdf_inventory.csv'
    manifest_path = inventory_dir / 'pdf_provenance.json'
    manifest = {}
    provenance_error = None
    if args.download:
        try:
            manifest = load_provenance(manifest_path)
            validate_previous_inventory(csv_path, manifest)
        except Exception as exc:
            provenance_error = f'{type(exc).__name__}: {exc}'
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
        for row in links:
            if not args.download:
                records.append(row)
                continue
            if args.limit and attempted >= args.limit:
                row['status'] = 'skipped_limit'
                records.append(row)
                continue
            attempted += 1
            try:
                if provenance_error:
                    raise ValueError(f'PDF provenance inventory unavailable: {provenance_error}')
                download_or_reuse(row, root, folder, manifest, manifest_path)
            except Exception as exc:
                row['status'] = 'file_error'
                row['error'] = f'{type(exc).__name__}: {exc}'
            records.append(row)
            time.sleep(0.65)
    if provenance_error and not records:
        records.append({'session': '', 'level': 'UNKNOWN', 'kind': 'UNCLASSIFIED',
                        'section': 'COMBINED_OR_UNKNOWN', 'label': '', 'url': '',
                        'source_page': '', 'status': 'file_error', 'file': '', 'sha256': '',
                        'error': provenance_error})
    keys = ['session', 'level', 'kind', 'section', 'label', 'url', 'source_page', 'status', 'file', 'sha256', 'error']
    report_errors = []
    try:
        output = io.StringIO(newline='')
        writer = csv.DictWriter(output, fieldnames=keys)
        writer.writeheader()
        writer.writerows(records)
        _atomic_write(csv_path, output.getvalue().encode('utf-8-sig'))
    except Exception as exc:
        report_errors.append(f'inventory write failed: {type(exc).__name__}: {exc}')
        print(report_errors[-1], file=sys.stderr)
    summary = {
        'source_count': sum(1 for _ in read_sources(sources)), 'candidate_count': sum(bool(r['url']) for r in records),
        'saved': sum(r['status'] == 'saved' for r in records),
        'already_present': sum(r['status'] == 'already_present' for r in records),
        'error_count': sum(r['status'].endswith('error') for r in records) + len(report_errors),
        'report_errors': report_errors,
        'pdfs_uploaded_to_github': 0,
    }
    try:
        _atomic_write(inventory_dir / 'collection_summary.json',
                      (json.dumps(summary, ensure_ascii=False, indent=2) + '\n').encode('utf-8'))
    except Exception as exc:
        report_errors.append(f'summary write failed: {type(exc).__name__}: {exc}')
        print(report_errors[-1], file=sys.stderr)
    print(f'Inventory: {csv_path}')
    print(f"PDFs saved this run: {sum(r['status'] == 'saved' for r in records)} / candidates {sum(bool(r['url']) for r in records)}")
    print('No Git operation performed; original PDFs are ignored by .gitignore.')
    return 2 if summary['error_count'] or report_errors else 0


if __name__ == '__main__':
    sys.exit(main())
