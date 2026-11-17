"""Unit tests for country filtering. No network and no speedtest-cli."""

import tempfile
import unittest
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
