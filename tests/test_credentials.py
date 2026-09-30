import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from kbar_download.envfile import load_credentials
from kbar_download.model import DataError
from kbar_download.cli import main


class CredentialTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / '.env'

    def test_bom_crlf_quotes_and_literal_special_characters(self):
        self.path.write_bytes(b'\xef\xbb\xbf# comment\r\nSJ_API_KEY="fake$KEY#x=y"\r\nSJ_SEC_KEY=\'fake\\secret\'\r\n')
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(load_credentials(self.path), {'SJ_API_KEY': 'fake$KEY#x=y', 'SJ_SEC_KEY': 'fake\\secret'})
            self.assertNotIn('SJ_API_KEY', os.environ)

    def test_environment_priority_and_missing_default(self):
        self.path.write_text('SJ_API_KEY=file-value\nSJ_SEC_KEY=file-secret\n')
        with patch.dict(os.environ, {'SJ_API_KEY': 'environment-value'}, clear=True):
            self.assertEqual(load_credentials(self.path)['SJ_API_KEY'], 'environment-value')
            self.path.unlink()
            self.assertEqual(load_credentials(self.path)['SJ_SEC_KEY'], '')

    def test_errors_never_include_values(self):
        for text in ['SJ_API_KEY="fake-sensitive', 'UNKNOWN=fake-sensitive',
                     'SJ_API_KEY=a\nSJ_API_KEY=fake-sensitive']:
            self.path.write_text(text)
            with self.assertRaises(DataError) as caught:
                load_credentials(self.path)
            self.assertNotIn('fake-sensitive', str(caught.exception))

    def test_offline_commands_do_not_read_credentials(self):
        config = Path(self.tmp.name) / 'config.json'
        config.write_text('{"output_dir":"data"}')
        with patch('kbar_download.cli.load_credentials', side_effect=AssertionError('must not read')):
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main(['doctor']), 0)
                self.assertEqual(main(['status', '--config', str(config)]), 0)
