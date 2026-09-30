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


class LoginDiagnosticTests(unittest.TestCase):
    def test_fixed_hints_never_echo_sensitive_exception_text(self):
        from kbar_download.provider import login_diagnostic
        for error, expected in [
            (TimeoutError('private-key private-secret'), 'timeout'),
            (ConnectionError('private-key private-secret'), 'connection'),
            (RuntimeError('timestamp private-key private-secret'), 'clock'),
            (RuntimeError('rate limit private-key private-secret'), 'rate_limit'),
            (RuntimeError('invalid api key private-key private-secret'), 'authentication'),
            (RuntimeError('private-key private-secret account=123 token=abc'), 'unknown'),
        ]:
            with self.subTest(expected=expected):
                result = login_diagnostic(error, 30.125)
                self.assertIn('線索=' + expected, result)
                self.assertIn('30.1s', result)
                for sensitive in ('private-key', 'private-secret', '123', 'abc'):
                    self.assertNotIn(sensitive, result)

    def test_connect_preserves_diagnostic_when_logout_also_fails(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        from kbar_download.provider import ShioajiProvider, SDK_VERSION
        from kbar_download.model import ProviderError
        api = Mock()
        api.login.side_effect = TimeoutError('private-key private-secret')
        api.logout.side_effect = RuntimeError('private-secret')
        sdk = SimpleNamespace(Shioaji=Mock(return_value=api))
        with patch.dict('sys.modules', {'shioaji': sdk}), \
                patch('kbar_download.provider.version', return_value=SDK_VERSION), \
                patch('kbar_download.provider.time.monotonic', side_effect=[10, 40]):
            with self.assertRaises(ProviderError) as caught:
                ShioajiProvider.connect(credentials={'SJ_API_KEY': 'private-key', 'SJ_SEC_KEY': 'private-secret'})
        self.assertIn('線索=timeout', str(caught.exception))
        self.assertIn('30.0s', str(caught.exception))
        self.assertNotIn('private-', str(caught.exception))
        api.login.assert_called_once()
        api.logout.assert_called_once()


    def test_sdk_types_without_recognizable_message(self):
        from kbar_download.provider import LOGIN_ERROR_TYPES, login_diagnostic
        for name, category in LOGIN_ERROR_TYPES.items():
            error_type = type(name, (Exception,), {'__module__': 'shioaji._core'})
            result = login_diagnostic(error_type('private-secret'), 0.1)
            self.assertIn('例外類型=' + name, result)
            self.assertIn('線索=' + category, result)
            self.assertNotIn('private-secret', result)

    def test_unknown_class_names_and_broken_messages_are_not_exposed(self):
        from kbar_download.provider import login_diagnostic
        private_type = type('private-secret', (ValueError,), {})
        result = login_diagnostic(private_type('private-secret'), 0.1)
        self.assertIn('例外類型=ValueError', result)
        self.assertNotIn('private-secret', result)

        class BrokenMessage(Exception):
            def __str__(self):
                raise ValueError('private-secret')

        result = login_diagnostic(BrokenMessage(), 0.1)
        self.assertIn('線索=unknown', result)
        self.assertNotIn('private-secret', result)
