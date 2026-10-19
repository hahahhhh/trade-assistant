import importlib.util
import json
import os
import time
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('smc_chart', ROOT / 'smc_chart.py')
sc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sc)


class SymbolTests(unittest.TestCase):
    def test_variety_maps_to_exchange_code(self):
        self.assertEqual(sc.symbol_for({'variety': 'V', 'contract': 'V2701'}), 'v2701')
        self.assertEqual(sc.symbol_for({'variety': 'JD', 'contract': 'JD2612'}), 'jd2612')
        self.assertEqual(sc.symbol_for({'variety': 'RB', 'contract': 'RB2701'}), 'rb2701')

    def test_label_is_used_when_contract_missing(self):
        self.assertEqual(sc.symbol_for({'variety': 'V', 'contract_label': 'PVC2701'}), 'v2701')

    def test_missing_inputs_raise(self):
        with self.assertRaises(ValueError):
            sc.symbol_for({})
        with self.assertRaises(ValueError):
            sc.symbol_for({'variety': 'V', 'contract': 'V27'})


class IndicatorTests(unittest.TestCase):
    def test_order_and_dedupe(self):
        self.assertEqual(sc.normalize_indicators(['fvg', 'liquidity', 'fvg']),
                         ['liquidity', 'fvg'])

    def test_unknown_indicator_raises(self):
        with self.assertRaises(ValueError):
            sc.normalize_indicators(['fvg', 'magic'])

    def test_empty_raises(self):
        with self.assertRaises(ValueError):
            sc.normalize_indicators([])

    def test_filename_is_order_independent(self):
        a = sc.chart_filename('jd2612', '5', ['liquidity', 'fvg'])
        b = sc.chart_filename('jd2612', '5', ['fvg', 'liquidity'])
        self.assertEqual(a, b)
        self.assertEqual(a, 'jd2612_5m_liquidity+fvg.png')


class CacheTests(unittest.TestCase):
    def test_missing_file(self):
        info = sc.cache_status(Path('/tmp/definitely-not-here.png'))
        self.assertFalse(info['exists'])
        self.assertTrue(info['stale'])

    def test_fresh_and_stale(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'x.png'
            path.write_bytes(b'png')
            fresh = sc.cache_status(path, ttl_seconds=600)
            self.assertTrue(fresh['exists'])
            self.assertFalse(fresh['stale'])
            old = time.time() - 3600
            os.utime(path, (old, old))
            stale = sc.cache_status(path, ttl_seconds=600)
            self.assertTrue(stale['stale'])
            self.assertIn('已过期', stale['label'])


class CommandTests(unittest.TestCase):
    def test_build_command(self):
        cmd = sc.build_runner_command(
            'C:/Python/python.exe', Path('C:/app/smc_chart_runner.py'),
            Path('D:/trade signals'), 'jd2612', '1440', ['fvg', 'liquidity'],
            200, 500, Path('C:/cache'),
        )
        self.assertIn('--root', cmd)
        self.assertIn('D:/trade signals', cmd)
        self.assertEqual(cmd[cmd.index('--period') + 1], '1440')
        self.assertIn('--indicators', cmd)
        self.assertEqual(cmd[cmd.index('--indicators') + 1], 'liquidity')
        self.assertEqual(cmd[cmd.index('--candles') + 1], '200')

    def test_parse_output_ignores_noise(self):
        payload = sc.parse_runner_output('warning\n{"ok": true, "png": "x.png"}\n')
        self.assertTrue(payload['ok'])
        self.assertEqual(payload['png'], 'x.png')

    def test_parse_output_raises_on_garbage(self):
        with self.assertRaises(ValueError):
            sc.parse_runner_output('no json here')

    def test_daily_period_detection(self):
        self.assertTrue(sc.is_daily_period('1440'))
        self.assertTrue(sc.is_daily_period(1440))
        self.assertTrue(sc.is_daily_period('1d'))
        for minute in ('5', '15', '60'):
            self.assertFalse(sc.is_daily_period(minute))

    def test_remote_command_quotes_paths(self):
        cmd = sc.remote_runner_command(
            '/home/coding/quant/bin/python', '/x/runner.py',
            '/home/coding/trade signal', 'jd2612', '5', ['fvg'], 200, 500, '/tmp/out')
        self.assertIn("'/home/coding/trade signal'", cmd)
        self.assertIn('--symbol jd2612', cmd)
        self.assertIn('--period 5', cmd)

    def test_ssh_command_is_non_interactive(self):
        cmd = sc.build_ssh_command('myhost', 'echo ok')
        self.assertEqual(cmd[0], 'ssh')
        self.assertIn('BatchMode=yes', cmd)
        self.assertEqual(cmd[-2:], ['myhost', 'echo ok'])

    def test_scp_command_targets_host_path(self):
        cmd = sc.build_scp_command('myhost', '/tmp/a.png', Path('/local/b.png'))
        self.assertEqual(cmd[0], 'scp')
        self.assertIn('myhost:/tmp/a.png', cmd)
        self.assertEqual(cmd[-1], '/local/b.png')

    def test_probe_command_quotes_python(self):
        cmd = sc.remote_probe_command('/opt/my python/bin/python')
        self.assertIn("'/opt/my python/bin/python'", cmd)
        self.assertIn('akshare', cmd)

    def test_scaled_size_fits_then_zooms(self):
        w, h = sc.scaled_size(2084, 1632, 1000, 700)
        self.assertEqual((w, h), (893, 700))
        w2, h2 = sc.scaled_size(2084, 1632, 1000, 700, 2.0)
        self.assertEqual((w2, h2), (1787, 1400))

    def test_scaled_size_rejects_bad_image(self):
        with self.assertRaises(ValueError):
            sc.scaled_size(0, 10, 100, 100)

    def test_read_bytes_with_retry_success(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'x.png'
            path.write_bytes(b'data')
            self.assertEqual(sc.read_bytes_with_retry(path), b'data')

    def test_read_bytes_with_retry_fails_after_attempts(self):
        with self.assertRaises(OSError):
            sc.read_bytes_with_retry(Path('/tmp/definitely-missing.png'), attempts=2, delay=0)

    def test_script_root(self):
        self.assertEqual(sc.script_root('/home/x/trade-signal/examples/a.py').name, 'trade-signal')


if __name__ == '__main__':
    unittest.main()
