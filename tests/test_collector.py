import unittest
import sys
import subprocess
import csv
import io
import json
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import collect_topik_pdfs as collector
from collect_topik_pdfs import LinkParser, file_category, level_from, unique_filename, inspect_page, main, read_bytes
from unittest.mock import patch

HTML = b'''<h3>96th TOPIK I (Beginner)</h3>
<a href="/TOPIK-Papers/96th-TOPIK-I-Reading-Test-Paper.pdf">READING TEST PAPER</a>
<h3>91st TOPIK II (mistyped heading)</h3>
<a href="/TOPIK-Papers/96th-TOPIK-II-Listening-Test-Paper.pdf">96th TOPIK II - LISTENING TEST PAPER</a>
<a href="/course">BUY NOW</a>
<a href="/test-answers">TOPIK II ANSWER KEYS</a>'''


def fake_pdf(identifier):
    return (b'%PDF-1.4\n1 0 obj\n<< /Type /Catalog /ID (' + identifier.encode('ascii') +
            b') >>\nendobj\ntrailer\n<< /Root 1 0 R >>\nstartxref\n0\n%%EOF\n')


def source_row(url, session=36):
    return {'session': session, 'level': 'I', 'kind': 'QUESTIONS',
            'section': 'READING', 'label': 'Reading test paper',
            'url': url, 'source_page': 'https://example.org/exams',
            'status': 'indexed', 'file': '', 'sha256': '', 'error': ''}


class CollectorRuns(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'source_pages.csv').write_text(
            'session,source_page\n36,https://example.org/exams\n', encoding='utf-8')
        self.url_a = 'https://example.org/A.pdf'
        self.url_b = 'https://example.org/B.pdf'
        self.bytes_by_url = {self.url_a: fake_pdf('a'), self.url_b: fake_pdf('b')}

    def run_collector(self, urls):
        def download(url, max_bytes):
            return self.bytes_by_url[url.split('#', 1)[0]]

        with patch.object(sys, 'argv', ['collect_topik_pdfs.py', '--root', str(self.root), '--download']), \
                patch('collect_topik_pdfs.inspect_page', return_value=[source_row(url) for url in urls]), \
                patch('collect_topik_pdfs.read_bytes', side_effect=download), \
                patch('collect_topik_pdfs.time.sleep'):
            result = main()
        with (self.root / 'catalog/pdf_inventory.csv').open(encoding='utf-8-sig', newline='') as stream:
            inventory = list(csv.DictReader(stream))
        summary = json.loads((self.root / 'catalog/collection_summary.json').read_text(encoding='utf-8'))
        return result, inventory, summary

    def test_order_change_never_reuses_another_urls_pdf(self):
        code, rows, _ = self.run_collector([self.url_a, self.url_b])
        self.assertEqual(code, 0)
        self.assertEqual([r['status'] for r in rows], ['saved', 'saved'])
        initial = {r['url']: (r['file'], r['sha256']) for r in rows}
        code, rows, summary = self.run_collector([self.url_b, self.url_a])
        self.assertEqual(code, 0)
        self.assertEqual([r['status'] for r in rows], ['already_present', 'already_present'])
        self.assertEqual(summary['error_count'], 0)
        self.assertEqual({r['url']: (r['file'], r['sha256']) for r in rows}, initial)
        for row in rows:
            self.assertEqual((self.root / row['file']).read_bytes(), self.bytes_by_url[row['url']])

    def test_modified_existing_pdf_is_rejected_without_overwriting(self):
        _, rows, _ = self.run_collector([self.url_a])
        destination = self.root / rows[0]['file']
        corrupted = b'%PDF-1.4\nbody missing the final trailer'
        destination.write_bytes(corrupted)
        code, rows, summary = self.run_collector([self.url_a])
        self.assertEqual(code, 2)
        self.assertEqual(rows[0]['status'], 'file_error')
        self.assertIn('PDF', rows[0]['error'])
        self.assertEqual(summary['error_count'], 1)
        self.assertEqual(destination.read_bytes(), corrupted)

    def test_existing_stable_path_without_provenance_is_not_trusted(self):
        row = source_row(self.url_a)
        path = self.root / 'local_sources/036/TOPIK_I' / unique_filename(row, 1)
        path.parent.mkdir(parents=True)
        path.write_bytes(fake_pdf('untrusted'))
        code, rows, summary = self.run_collector([self.url_a])
        self.assertEqual(code, 2)
        self.assertEqual(rows[0]['status'], 'file_error')
        self.assertIn('provenance', rows[0]['error'].lower())
        self.assertEqual(summary['error_count'], 1)
        self.assertEqual(path.read_bytes(), fake_pdf('untrusted'))

    def test_truncated_download_is_rejected_then_retry_is_saved(self):
        self.bytes_by_url[self.url_a] = b'%PDF-1.4\nmissing EOF'
        code, rows, summary = self.run_collector([self.url_a])
        self.assertEqual(code, 2)
        self.assertEqual(rows[0]['status'], 'file_error')
        self.assertEqual(summary['error_count'], 1)
        self.assertEqual(list((self.root / 'local_sources').rglob('*.pdf')) if (self.root / 'local_sources').exists() else [], [])
        self.bytes_by_url[self.url_a] = fake_pdf('restored')
        code, rows, _ = self.run_collector([self.url_a])
        self.assertEqual(code, 0)
        self.assertEqual(rows[0]['status'], 'saved')

    def test_download_error_is_reported_and_recoverable(self):
        self.bytes_by_url.clear()
        code, rows, summary = self.run_collector([self.url_a])
        self.assertEqual(code, 2)
        self.assertEqual(rows[0]['status'], 'file_error')
        self.assertEqual(summary['error_count'], 1)
        self.bytes_by_url[self.url_a] = fake_pdf('a')
        code, rows, summary = self.run_collector([self.url_a])
        self.assertEqual((code, rows[0]['status'], summary['error_count']), (0, 'saved', 0))

    def test_removed_then_readded_url_retains_provenance(self):
        self.run_collector([self.url_a])
        self.run_collector([])
        code, rows, _ = self.run_collector([self.url_a])
        self.assertEqual(code, 0)
        self.assertEqual(rows[0]['status'], 'already_present')

    def test_duplicate_canonical_url_reuses_only_matching_hash(self):
        code, rows, _ = self.run_collector([self.url_a + '#first', self.url_a + '#second'])
        self.assertEqual(code, 0)
        self.assertEqual([row['status'] for row in rows], ['saved', 'already_present'])
        self.assertEqual(rows[0]['file'], rows[1]['file'])

    def test_previous_inventory_conflict_is_reported(self):
        self.run_collector([self.url_a])
        inventory = self.root / 'catalog/pdf_inventory.csv'
        with inventory.open(encoding='utf-8-sig', newline='') as stream:
            rows = list(csv.DictReader(stream))
        rows[0]['sha256'] = '0' * 64
        with inventory.open('w', encoding='utf-8-sig', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows(rows)
        code, rows, summary = self.run_collector([self.url_a])
        self.assertEqual(code, 2)
        self.assertEqual(rows[0]['status'], 'file_error')
        self.assertIn('inventory', rows[0]['error'])
        self.assertEqual(summary['error_count'], 1)

    def test_atomic_pdf_publish_failure_removes_temporary_file(self):
        with patch('collect_topik_pdfs.os.link', side_effect=FileExistsError('target race')):
            code, rows, summary = self.run_collector([self.url_a])
        self.assertEqual(code, 2)
        self.assertEqual(rows[0]['status'], 'file_error')
        self.assertEqual(summary['error_count'], 1)
        self.assertFalse(list(self.root.rglob('*.tmp')))
        self.assertFalse(list(self.root.rglob('*.pdf')))
        code, rows, _ = self.run_collector([self.url_a])
        self.assertEqual((code, rows[0]['status']), (0, 'saved'))

    def test_inventory_write_error_is_reflected_in_summary_and_exit_code(self):
        original_write = collector._atomic_write

        def fail_inventory(path, content):
            if path.name == 'pdf_inventory.csv':
                raise OSError('read-only catalog')
            return original_write(path, content)

        with patch.object(sys, 'argv', ['collect_topik_pdfs.py', '--root', str(self.root), '--download']), \
                patch('collect_topik_pdfs.inspect_page', return_value=[source_row(self.url_a)]), \
                patch('collect_topik_pdfs.read_bytes', return_value=fake_pdf('a')), \
                patch('collect_topik_pdfs.time.sleep'), \
                patch('collect_topik_pdfs._atomic_write', side_effect=fail_inventory):
            result = main()
        self.assertEqual(result, 2)
        self.assertFalse((self.root / 'catalog/pdf_inventory.csv').exists())
        summary = json.loads((self.root / 'catalog/collection_summary.json').read_text(encoding='utf-8'))
        self.assertEqual(summary['error_count'], 1)
        self.assertIn('inventory write failed', summary['report_errors'][0])

    def test_replaced_but_valid_existing_pdf_fails_hash_check(self):
        _, rows, _ = self.run_collector([self.url_a])
        destination = self.root / rows[0]['file']
        destination.write_bytes(fake_pdf('different-valid-pdf'))
        code, rows, _ = self.run_collector([self.url_a])
        self.assertEqual(code, 2)
        self.assertEqual(rows[0]['status'], 'file_error')
        self.assertIn('SHA-256', rows[0]['error'])
        self.assertEqual(destination.read_bytes(), fake_pdf('different-valid-pdf'))

    def test_manifest_mapping_conflict_prevents_reuse(self):
        _, rows, _ = self.run_collector([self.url_a])
        destination = self.root / rows[0]['file']
        manifest = self.root / 'catalog/pdf_provenance.json'
        content = json.loads(manifest.read_text(encoding='utf-8'))
        entry = next(iter(content['records'].values()))
        entry['sha256'] = '0' * 64
        manifest.write_text(json.dumps(content), encoding='utf-8')
        code, rows, summary = self.run_collector([self.url_a])
        self.assertEqual(code, 2)
        self.assertEqual(rows[0]['status'], 'file_error')
        self.assertEqual(summary['error_count'], 1)
        self.assertEqual(destination.read_bytes(), fake_pdf('a'))

    def test_legacy_sequential_file_does_not_get_reused(self):
        legacy = self.root / 'local_sources/036/TOPIK_I/TOPIK_036_I_READING_QUESTIONS_01.pdf'
        legacy.parent.mkdir(parents=True)
        legacy.write_bytes(fake_pdf('legacy'))
        code, rows, _ = self.run_collector([self.url_a])
        self.assertEqual(code, 0)
        self.assertEqual(rows[0]['status'], 'saved')
        self.assertNotEqual(rows[0]['file'], legacy.relative_to(self.root).as_posix())
        self.assertEqual(legacy.read_bytes(), fake_pdf('legacy'))

class TestCollector(unittest.TestCase):
    def test_level(self):
        self.assertEqual(level_from('TOPIK II'), 'II')
        self.assertEqual(level_from('TOPIK I'), 'I')
        self.assertIsNone(level_from('practice'))
    def test_categories(self):
        self.assertEqual(file_category('READING TEST PAPER'), ('QUESTIONS', 'READING'))
        self.assertEqual(file_category('LISTENING TRANSCRIPT'), ('TRANSCRIPT', 'LISTENING'))
    @patch('collect_topik_pdfs.read_bytes', return_value=HTML)
    def test_page(self, fake):
        result = inspect_page(96, 'https://www.topikguide.com/96/')
        self.assertEqual(len(result), 3)
        self.assertEqual(result[0]['level'], 'I')
        self.assertEqual(result[1]['level'], 'II')
        self.assertEqual(result[2]['kind'], 'ANSWER_KEY')
        self.assertIn('96th-TOPIK-I-Reading-Test-Paper.pdf', result[0]['url'])
    def test_name(self):
        item = {'session': 35, 'level': 'II', 'section': 'READING', 'kind': 'QUESTIONS',
                'url': 'HTTPS://Example.org/a.pdf#first'}
        name = unique_filename(item, 2)
        self.assertTrue(name.startswith('TOPIK_035_II_READING_QUESTIONS_'))
        self.assertTrue(name.endswith('.pdf'))
        self.assertEqual(name, unique_filename(item, 5))
        self.assertEqual(name, unique_filename({**item, 'url': 'https://example.org/a.pdf#second'}, 1))
        self.assertEqual(name, unique_filename({**item, 'url': 'https://example.org:443/a.pdf'}, 1))
        self.assertNotEqual(name, unique_filename({**item, 'url': 'https://example.org/b.pdf'}, 2))
        self.assertNotEqual(name, unique_filename({**item, 'url': 'https://example.org/a.pdf?edition=2'}, 2))

    @patch('collect_topik_pdfs.urlopen')
    def test_short_content_length_response_rejected(self, fake_urlopen):
        class Response(io.BytesIO):
            def __init__(self):
                super().__init__(fake_pdf('partial'))
                self.headers = {'Content-Length': str(len(fake_pdf('partial')) + 30)}
        fake_urlopen.return_value = Response()
        with self.assertRaisesRegex(ValueError, 'incomplete|truncat|length'):
            read_bytes('https://example.org/source.pdf', 1024)

    def test_cli_help(self):
        script = Path(__file__).resolve().parents[1] / 'collect_topik_pdfs.py'
        result = subprocess.run([sys.executable, str(script), '--help'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--download', result.stdout)

if __name__ == '__main__':
    unittest.main()
