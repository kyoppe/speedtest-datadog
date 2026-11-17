"""Unit tests for country filtering. No network and no speedtest-cli."""

import io
import json
import os
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import speedtest_runner


def _server(server_id, cc, country, distance, name='Tokyo'):
    return {
        'id': server_id,
        'cc': cc,
        'country': country,
        'd': distance,
        'sponsor': 'Example',
        'name': name,
    }


class MetricNamingTest(unittest.TestCase):
    def test_production_metric_names_are_stable(self):
        self.assertEqual(
            speedtest_runner.resolve_metric_name('kyouhei', 'download'),
            'kyouhei.speedtest.download',
        )
        self.assertEqual(
            speedtest_runner.resolve_metric_name('kyouhei', 'test_count'),
            'kyouhei.speedtest.test_count',
        )

    def test_production_literals_are_present_at_metric_send_calls(self):
        source = Path(speedtest_runner.__file__).read_text(encoding='utf-8')
        for metric_name in (
            'kyouhei.speedtest.download',
            'kyouhei.speedtest.upload',
            'kyouhei.speedtest.ping',
            'kyouhei.speedtest.test_count',
        ):
            self.assertIn(f'metric="{metric_name}"', source)

    def test_test_mode_and_custom_prefixes_are_dynamic(self):
        self.assertEqual(
            speedtest_runner.resolve_metric_prefix(None, metrics_test_mode=True),
            'test.kyouhei',
        )
        self.assertEqual(
            speedtest_runner.resolve_metric_name('test.kyouhei', 'download'),
            'test.kyouhei.speedtest.download',
        )
        self.assertEqual(
            speedtest_runner.resolve_metric_name('customer', 'download'),
            'customer.speedtest.download',
        )
        self.assertEqual(
            speedtest_runner.resolve_metric_name('customer.speedtest', 'download'),
            'customer.speedtest.speedtest.download',
        )

    def test_explicit_prefix_takes_precedence_over_test_mode(self):
        self.assertEqual(
            speedtest_runner.resolve_metric_prefix('customer', metrics_test_mode=True),
            'customer',
        )

    def test_empty_prefix_uses_default_or_test_mode(self):
        self.assertEqual(
            speedtest_runner.resolve_metric_prefix('', metrics_test_mode=False),
            'kyouhei',
        )
        self.assertEqual(
            speedtest_runner.resolve_metric_prefix('  ', metrics_test_mode=True),
            'test.kyouhei',
        )


class MetricSendTest(unittest.TestCase):
    def test_production_metrics_are_sent_with_expected_names(self):
        sent_metrics = []
        fake_api = types.SimpleNamespace(
            Metric=types.SimpleNamespace(
                send=lambda **kwargs: sent_metrics.append(kwargs),
            ),
        )
        fake_datadog = types.SimpleNamespace(
            initialize=mock.Mock(),
            api=fake_api,
        )
        metrics = {
            'download': 100.0,
            'upload': 50.0,
            'ping': 10.0,
        }

        with mock.patch.dict(sys.modules, {'datadog': fake_datadog}):
            speedtest_runner.send_to_datadog(
                metrics,
                [],
                'kyouhei',
                'api-key',
                'datadoghq.com',
            )

        self.assertEqual(
            [metric['metric'] for metric in sent_metrics],
            [
                'kyouhei.speedtest.download',
                'kyouhei.speedtest.upload',
                'kyouhei.speedtest.ping',
                'kyouhei.speedtest.test_count',
            ],
        )


class StructuredLoggingTest(unittest.TestCase):
    def test_log_includes_version_and_trace_context(self):
        span = mock.Mock(trace_id=123, span_id=456)
        tracer = mock.Mock()
        tracer.current_span.return_value = span
        output = io.StringIO()

        with mock.patch.object(speedtest_runner, 'tracer', tracer), \
                mock.patch.object(speedtest_runner, 'DDTRACE_AVAILABLE', True), \
                mock.patch.dict(os.environ, {'DD_VERSION': 'abc123'}, clear=False), \
                mock.patch.object(speedtest_runner, '_JSON_OUTPUT_STREAM', None), \
                redirect_stdout(output):
            speedtest_runner.log_json(
                None,
                'info',
                'test message',
                event_type='test_event',
            )

        entry = json.loads(output.getvalue())
        self.assertEqual(entry['event_type'], 'test_event')
        self.assertEqual(entry['dd.version'], 'abc123')
        self.assertEqual(entry['dd.trace_id'], '123')
        self.assertEqual(entry['dd.span_id'], '456')

    def test_plain_print_is_json_when_output_is_enabled(self):
        output = io.StringIO()
        original_stdout = sys.stdout
        try:
            with redirect_stdout(output):
                speedtest_runner.enable_json_output()
                print('plain output')
                sys.stdout.flush()
        finally:
            sys.stdout = original_stdout
            speedtest_runner._JSON_OUTPUT_STREAM = None

        entry = json.loads(output.getvalue())
        self.assertEqual(entry['event_type'], 'console_output')
        self.assertEqual(entry['message'], 'plain output')


class SelectServersByCountryTest(unittest.TestCase):
    def test_keeps_matching_country_and_sorts_by_distance(self):
        servers = {
            1.0: [
                _server('far', 'JP', 'Japan', 400, 'Osaka'),
                _server('ru', 'RU', 'Russia', 5, 'Khabarovsk'),
            ],
            999.0: [_server('near', 'jp', 'Japan', 10, 'Tokyo')],
        }

        chosen = speedtest_runner.select_servers_by_country(servers, 'JP')

        self.assertEqual([server['id'] for server in chosen], ['near', 'far'])

    def test_matches_country_name(self):
        servers = {1.0: [_server('jp', 'JP', 'Japan', 1)]}

        chosen = speedtest_runner.select_servers_by_country(servers, 'Japan')

        self.assertEqual([server['id'] for server in chosen], ['jp'])

    def test_blank_country_matches_nothing(self):
        servers = {1.0: [_server('jp', 'JP', 'Japan', 1)]}

        self.assertEqual(speedtest_runner.select_servers_by_country(servers, '  '), [])


class ReadCountryCodeTest(unittest.TestCase):
    def test_missing_file_is_unset(self):
        self.assertIsNone(speedtest_runner.read_country_code('/no/such/speedtest-country'))

    def test_blank_file_is_unset(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'speedtest-country'
            path.write_text('\n  \n# comment\n', encoding='utf-8')

            self.assertIsNone(speedtest_runner.read_country_code(str(path)))

    def test_directory_path_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(Exception) as raised:
                speedtest_runner.read_country_code(tmp)

        self.assertIn('is a directory', str(raised.exception))

    def test_reads_first_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'speedtest-country'
            path.write_text('# note\n  JP  \n', encoding='utf-8')

            self.assertEqual(speedtest_runner.read_country_code(str(path)), 'JP')


class CountrySpeedtestTest(unittest.TestCase):
    def test_tries_next_server_in_country_and_skips_auto(self):
        servers = {
            10.0: [_server('10', 'JP', 'Japan', 10)],
            20.0: [_server('11', 'JP', 'Japan', 20, 'Tsukuba')],
        }
        calls = []

        def fake_execute(server_id=None, timeout=120):
            calls.append(server_id)
            if server_id == '10':
                raise speedtest_runner.SpeedtestAttemptError('down')
            return {
                'download': 1_000_000,
                'upload': 1_000_000,
                'ping': 1,
                'server': {},
            }

        with mock.patch.object(speedtest_runner, 'fetch_servers_by_distance', return_value=servers), \
                mock.patch.object(speedtest_runner, 'execute_speedtest', side_effect=fake_execute):
            data, selection = speedtest_runner.run_speedtest(country_code='JP')

        self.assertEqual(calls, ['10', '11'])
        self.assertEqual(selection, 'country')
        self.assertEqual(data['ping'], 1)

    def test_exhausted_country_does_not_fall_back_to_auto(self):
        servers = {10.0: [_server('10', 'JP', 'Japan', 10)]}
        calls = []

        def fake_execute(server_id=None, timeout=120):
            calls.append(server_id)
            raise speedtest_runner.SpeedtestAttemptError('down')

        with mock.patch.object(speedtest_runner, 'fetch_servers_by_distance', return_value=servers), \
                mock.patch.object(speedtest_runner, 'execute_speedtest', side_effect=fake_execute):
            with self.assertRaises(Exception) as raised:
                speedtest_runner.run_speedtest(country_code='JP')

        self.assertEqual(calls, ['10'])
        self.assertIn('All JP speedtest servers failed', str(raised.exception))


if __name__ == '__main__':
    unittest.main()
