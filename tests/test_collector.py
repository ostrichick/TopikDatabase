import unittest
import sys
import subprocess
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from collect_topik_pdfs import LinkParser, file_category, level_from, unique_filename, inspect_page
from unittest.mock import patch

HTML = b'''<h3>96th TOPIK I (Beginner)</h3>
<a href="/TOPIK-Papers/96th-TOPIK-I-Reading-Test-Paper.pdf">READING TEST PAPER</a>
<h3>91st TOPIK II (mistyped heading)</h3>
<a href="/TOPIK-Papers/96th-TOPIK-II-Listening-Test-Paper.pdf">96th TOPIK II - LISTENING TEST PAPER</a>
<a href="/course">BUY NOW</a>
<a href="/test-answers">TOPIK II ANSWER KEYS</a>'''

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
        item = {'session': 35, 'level': 'II', 'section': 'READING', 'kind': 'QUESTIONS'}
        self.assertEqual(unique_filename(item, 2), 'TOPIK_035_II_READING_QUESTIONS_02.pdf')

    def test_cli_help(self):
        script = Path(__file__).resolve().parents[1] / 'collect_topik_pdfs.py'
        result = subprocess.run([sys.executable, str(script), '--help'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--download', result.stdout)

if __name__ == '__main__':
    unittest.main()
