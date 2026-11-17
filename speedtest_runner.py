#!/usr/bin/env python3
"""
Speedtest to Datadog Metrics Exporter
定期的にインターネット速度テストを実行し、結果をDatadogに送信します。
"""

import os
import sys
import json
import argparse
import subprocess
import logging
import time
import signal
import socket
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

# .envファイルがあれば読み込み
load_dotenv()

# ddtraceは存在する場合のみインポート（オプショナル）
try:
    from ddtrace import tracer, patch
    DDTRACE_AVAILABLE = True
    # Datadog Agentとの通信を試みる（localhost:8126）
    # 設定はデフォルト（環境変数DD_AGENT_HOSTなどで上書き可能）
except ImportError:
    DDTRACE_AVAILABLE = False
    tracer = None

# Graceful shutdown flag
shutdown_flag = False
_JSON_OUTPUT_STREAM = None
_ACTIVE_LOGGER = None


def parse_boolean(value):
    """Parse a human-friendly boolean environment value."""
    return str(value).strip().lower() in {'1', 'true', 'yes', 'on'}


def resolve_metric_prefix(explicit_prefix=None, metrics_test_mode=False):
    """Resolve the base prefix while preserving custom prefix compatibility."""
    if explicit_prefix and explicit_prefix.strip():
        return explicit_prefix.strip()
    if metrics_test_mode:
        return 'test.kyouhei'
    return 'kyouhei'


def resolve_metric_name(prefix, metric_name):
    """Resolve test and custom metric names for display and dynamic sends."""
    return f'{prefix}.speedtest.{metric_name}'


def _trace_context():
    """Return Datadog trace identifiers for the currently active span."""
    if not DDTRACE_AVAILABLE or not tracer:
        return {}
    try:
        span = tracer.current_span()
    except Exception:
        return {}
    if not span:
        return {}
    return {
        'dd.trace_id': str(span.trace_id),
        'dd.span_id': str(span.span_id),
    }


def _write_json_log(log_entry):
    output = _JSON_OUTPUT_STREAM or sys.stdout
    output.write(json.dumps(log_entry, ensure_ascii=False) + '\n')
    output.flush()


class JsonOutput:
    """Convert ordinary stdout lines into structured JSON log events."""

    def __init__(self, output, stream_name='stdout'):
        self.output = output
        self.stream_name = stream_name
        self.buffer = ''

    def write(self, data):
        self.buffer += data
        while '\n' in self.buffer:
            line, self.buffer = self.buffer.split('\n', 1)
            if line.strip():
                log_json(
                    _ACTIVE_LOGGER,
                    'info',
                    line,
                    event_type='console_output',
                    stream=self.stream_name,
                )
        return len(data)

    def flush(self):
        if self.buffer.strip():
            log_json(
                _ACTIVE_LOGGER,
                'info',
                self.buffer,
                event_type='console_output',
                stream=self.stream_name,
            )
            self.buffer = ''
        self.output.flush()

    def isatty(self):
        return self.output.isatty()

    @property
    def encoding(self):
        return self.output.encoding


def enable_json_output():
    """Wrap stdout so legacy print calls remain structured when logs are enabled."""
    global _JSON_OUTPUT_STREAM
    if isinstance(sys.stdout, JsonOutput):
        return
    _JSON_OUTPUT_STREAM = sys.stdout
    sys.stdout = JsonOutput(_JSON_OUTPUT_STREAM)

def signal_handler(signum, frame):
    """Handle shutdown signals gracefully"""
    global shutdown_flag
    print("\n🛑 Shutdown signal received, stopping after current test...")
    shutdown_flag = True


def setup_json_logging():
    """JSON形式のロガーをセットアップ"""
    logger = logging.getLogger('speedtest')
    logger.setLevel(logging.INFO)
    
    # JSON形式のフォーマッター
    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(logging.INFO)
    
    # 標準出力には通常のフォーマット（コンテナログ用）
    logger.addHandler(handler)
    
    return logger


def log_json(logger, level, message, **extra_fields):
    """JSON形式でログを出力（構造化ログ）"""
    event_type = extra_fields.pop('event_type', 'speedtest_event')
    log_entry = {
        "timestamp": datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
        "service": "speedtest",
        "message": message,
        "level": level,
        "event_type": event_type,
        "dd.version": os.getenv('DD_VERSION', 'unknown'),
        **extra_fields
    }
    log_entry.update(_trace_context())
    _write_json_log(log_entry)


def get_host_info():
    """Get hostname, container ID, and container name"""
    # Get host hostname (passed via env var, or use local hostname)
    host_hostname = os.getenv('HOST_HOSTNAME')
    
    # Get container info
    container_name = os.getenv('CONTAINER_NAME')
    
    # If CONTAINER_NAME is set, we're in a container
    if container_name:
        # Try to get full container ID from /proc/self/mountinfo
        container_id = None
        try:
            with open('/proc/self/mountinfo', 'r') as f:
                for line in f:
                    if '/docker/containers/' in line:
                        # Extract full container ID from path like: /docker/containers/<full_id>/...
                        parts = line.split('/docker/containers/')
                        if len(parts) > 1:
                            container_id = parts[1].split('/')[0]
                            if len(container_id) == 64:  # Full container ID
                                break
        except (FileNotFoundError, IOError):
            pass
        
        # Fallback to hostname (short container ID) if full ID not found
        if not container_id:
            container_id = socket.gethostname()
        
        # Use HOST_HOSTNAME if set, otherwise fall back to container hostname (not ideal)
        hostname = host_hostname if host_hostname else socket.gethostname()
    else:
        # Not in container, use local hostname
        hostname = host_hostname if host_hostname else socket.gethostname()
        container_id = None
        container_name = None
    
    return hostname, container_id, container_name


DEFAULT_COUNTRY_FILE = '/config/speedtest-country'


class SpeedtestAttemptError(Exception):
    """One speedtest attempt failed and another candidate may be tried."""


def read_country_code(path):
    """Return the country code or name from a file, or None when unset.

    The file is read on every call so a bind-mounted file can change the
    target without restarting the process. A missing or blank file means
    no country filter.
    """
    if not path:
        return None
    try:
        with open(path, 'r', encoding='utf-8') as handle:
            for line in handle:
                text = line.strip()
                if text and not text.startswith('#'):
                    return text.split()[0]
    except FileNotFoundError:
        return None
    except IsADirectoryError as exc:
        raise Exception(
            f"{path} is a directory. Mount a file that contains a country code."
        ) from exc
    return None


def select_servers_by_country(servers_by_distance, country):
    """Return servers whose country code or name matches, nearest first."""
    wanted = country.strip().casefold()
    if not wanted:
        return []

    matched = []
    for servers in servers_by_distance.values():
        for server in servers:
            cc = str(server.get('cc', '')).casefold()
            name = str(server.get('country', '')).casefold()
            if wanted == cc or wanted == name:
                matched.append(server)

    matched.sort(key=lambda server: float(server.get('d', float('inf'))))
    return matched


def fetch_servers_by_distance():
    """Load the speedtest.net server list keyed by distance."""
    import speedtest as speedtest_mod

    try:
        client = speedtest_mod.Speedtest()
        return client.get_servers()
    except Exception as exc:
        raise SpeedtestAttemptError(f'server list failed: {exc}') from exc


def execute_speedtest(server_id=None, timeout=120):
    """Run one speedtest-cli attempt and return the parsed JSON."""
    cmd = ['speedtest-cli', '--json']
    if server_id is not None:
        cmd.extend(['--server', str(server_id)])

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
            timeout=timeout,
        )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or str(exc)).strip()
        raise SpeedtestAttemptError(detail or 'speedtest failed') from exc
    except subprocess.TimeoutExpired as exc:
        raise SpeedtestAttemptError('timeout') from exc

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise SpeedtestAttemptError(f'invalid json: {exc}') from exc


def _finish_speedtest_span(span, data, server_selection):
    if not span:
        return
    span.set_tag("speedtest.download_mbps", round(data['download'] / 1_000_000, 2))
    span.set_tag("speedtest.upload_mbps", round(data['upload'] / 1_000_000, 2))
    span.set_tag("speedtest.ping_ms", round(data['ping'], 2))
    span.set_tag("speedtest.server_selection", server_selection)
    span.finish()


def _log_speedtest_success(logger, data, server_selection):
    print("✅ Speed test completed")
    if logger:
        log_json(logger, "info", "Speedtest completed",
                event_type="speedtest_completed",
                status="success",
                download_mbps=round(data['download'] / 1_000_000, 2),
                upload_mbps=round(data['upload'] / 1_000_000, 2),
                ping_ms=round(data['ping'], 2),
                server_selection=server_selection)


def run_speedtest(logger=None, enable_tracing=False, server_id=None, country_code=None):
    """Run speedtest-cli and return results as JSON plus the selection mode."""

    span = None
    if enable_tracing and DDTRACE_AVAILABLE and tracer:
        span = tracer.trace("speedtest.run", service="speedtest")
        if country_code:
            span.set_tag("speedtest.country_requested", country_code)
        elif server_id:
            span.set_tag("speedtest.server_id_requested", server_id)

    if country_code:
        return _run_speedtest_for_country(logger, span, country_code)

    return _run_speedtest_for_server_id(logger, span, server_id)


def _run_speedtest_for_country(logger, span, country_code):
    """Try servers in one country, nearest first. Never fall back to auto."""
    try:
        servers = fetch_servers_by_distance()
    except SpeedtestAttemptError as exc:
        error_msg = f"Speed test server list failed for country {country_code}: {exc}"
        print(f"❌ {error_msg}")
        if logger:
            log_json(logger, "error", error_msg,
                    event_type="speedtest_country_unavailable",
                    status="failed", country=country_code, error=str(exc))
        if span:
            span.set_tag("error", True)
            span.set_tag("error.message", error_msg)
            span.finish()
        raise Exception(error_msg) from exc

    candidates = select_servers_by_country(servers, country_code)
    if not candidates:
        error_msg = f"No speedtest servers for country {country_code}"
        print(f"❌ {error_msg}")
        if logger:
            log_json(logger, "error", error_msg,
                    event_type="speedtest_country_unavailable",
                    status="failed", country=country_code)
        if span:
            span.set_tag("error", True)
            span.set_tag("error.message", error_msg)
            span.finish()
        raise Exception(error_msg)

    print(f"🌏 Country filter: {country_code} ({len(candidates)} servers, nearest first)")
    errors = []
    for server in candidates:
        server_id = str(server.get('id'))
        distance_km = float(server.get('d', 0))
        label = (
            f"{server_id} {server.get('sponsor', '')} "
            f"({server.get('name', '')}) {distance_km:.1f} km"
        )
        print(f"🚀 Running speed test (country {country_code}, server {label})...")
        if logger:
            log_json(logger, "info", "Starting speedtest",
                    event_type="speedtest_started",
                    status="running",
                    country=country_code,
                    server_id=server_id,
                    server_name=server.get('name'),
                    server_sponsor=server.get('sponsor'),
                    distance_km=round(distance_km, 2),
                    server_selection="country")
        try:
            data = execute_speedtest(server_id)
        except FileNotFoundError:
            error_msg = "speedtest-cli not installed"
            print(f"❌ {error_msg}")
            print("   Install: pip install speedtest-cli")
            if logger:
                log_json(logger, "error", error_msg,
                        event_type="speedtest_cli_missing",
                        status="failed")
            if span:
                span.set_tag("error", True)
                span.set_tag("error.message", error_msg)
                span.finish()
            raise Exception(error_msg)
        except SpeedtestAttemptError as exc:
            errors.append(f"{server_id}: {exc}")
            print(f"⚠️  Server {server_id} failed, trying the next {country_code} server")
            if logger:
                log_json(logger, "warning", "Speedtest server failed",
                        event_type="speedtest_server_failed",
                        status="retry",
                        country=country_code,
                        server_id=server_id,
                        error=str(exc))
            continue

        _log_speedtest_success(logger, data, 'country')
        _finish_speedtest_span(span, data, 'country')
        return data, 'country'

    error_msg = (
        f"All {country_code} speedtest servers failed: " + '; '.join(errors)
    )
    print(f"❌ {error_msg}")
    if logger:
        log_json(logger, "error", error_msg,
                event_type="speedtest_country_exhausted",
                status="failed",
                country=country_code,
                error=error_msg)
    if span:
        span.set_tag("error", True)
        span.set_tag("error.message", error_msg)
        span.finish()
    raise Exception(error_msg)


def _run_speedtest_for_server_id(logger, span, server_id):
    """Run one server id, or auto selection. A failed id falls back to auto."""
    server_selection = 'manual' if server_id else 'auto'
    if server_id:
        print(f"🚀 Running speed test (server ID: {server_id})...")
    else:
        print("🚀 Running speed test (auto server selection)...")
    if logger:
        log_json(logger, "info", "Starting speedtest",
                event_type="speedtest_started",
                status="running",
                server_id=server_id,
                server_selection=server_selection)

    try:
        data = execute_speedtest(server_id)
    except FileNotFoundError:
        error_msg = "speedtest-cli not installed"
        print(f"❌ {error_msg}")
        print("   Install: pip install speedtest-cli")
        if logger:
            log_json(logger, "error", error_msg,
                    event_type="speedtest_cli_missing",
                    status="failed")
        if span:
            span.set_tag("error", True)
            span.set_tag("error.message", error_msg)
            span.finish()
        raise Exception(error_msg)
    except SpeedtestAttemptError as exc:
        if server_id:
            error_msg = f"Speed test with server {server_id} failed, falling back to auto selection"
            print(f"⚠️  {error_msg}")
            if logger:
                log_json(logger, "warning", error_msg,
                        event_type="speedtest_server_fallback",
                        status="fallback",
                        server_id=server_id,
                        error=str(exc))
            if span:
                span.set_tag("speedtest.fallback", True)
                span.finish()
            return run_speedtest(logger=logger, enable_tracing=False, server_id=None)

        error_msg = f"Speed test failed: {exc}"
        print(f"❌ {error_msg}")
        if logger:
            log_json(logger, "error", error_msg,
                    event_type="speedtest_failed",
                    status="failed",
                    error_type="process_error")
        if span:
            span.set_tag("error", True)
            span.set_tag("error.message", error_msg)
            span.finish()
        raise Exception(error_msg) from exc

    _log_speedtest_success(logger, data, server_selection)
    _finish_speedtest_span(span, data, server_selection)
    return data, server_selection


def parse_speedtest_results(data, server_selection='auto'):
    """Parse speedtest results and generate metrics and tags"""
    # Metrics (convert to Mbps)
    metrics = {
        'download': data['download'] / 1_000_000,  # bps -> Mbps
        'upload': data['upload'] / 1_000_000,      # bps -> Mbps
        'ping': data['ping']                        # ms
    }
    
    # Get host information
    hostname, container_id, container_name = get_host_info()
    
    # Server information as tags
    server = data.get('server', {})
    tags = [
        f"host:{hostname}",  # Use actual hostname
        f"speedtest_server_name:{server.get('name', 'unknown')}",
        f"speedtest_server_country:{server.get('country', 'unknown')}",
        f"speedtest_server_sponsor:{server.get('sponsor', 'unknown')}",
        f"speedtest_server_id:{server.get('id', 'unknown')}",
        f"server_selection:{server_selection}",
    ]
    
    # Add container tags if running in container
    if container_id:
        tags.append(f"container_id:{container_id}")
    if container_name:
        tags.append(f"container_name:{container_name}")
    
    # Client information (optional)
    client = data.get('client', {})
    if client.get('isp'):
        tags.append(f"isp:{client['isp']}")
    
    return metrics, tags


def send_to_datadog(metrics, tags, prefix, api_key, site, logger=None, enable_tracing=False):
    """Send metrics and tags to Datadog"""
    
    # Start tracing span
    span = None
    if enable_tracing and DDTRACE_AVAILABLE and tracer:
        span = tracer.trace("datadog.send_metrics", service="speedtest")
    
    try:
        from datadog import initialize, api
        
        options = {
            'api_key': api_key,
            'api_host': f'https://api.{site}'
        }
        initialize(**options)
        
        timestamp = int(datetime.now().timestamp())
        
        # Send metrics
        print("\n📊 Sending metrics:")
        if prefix == 'kyouhei':
            # Keep production metric names as literals at the API call sites so
            # Datadog Metrics Catalog can link them back to this source code.
            api.Metric.send(
                metric="kyouhei.speedtest.download",
                points=[(timestamp, metrics['download'])],
                tags=tags
            )
            print(f"  📤 kyouhei.speedtest.download = {metrics['download']:.2f} Mbps")

            api.Metric.send(
                metric="kyouhei.speedtest.upload",
                points=[(timestamp, metrics['upload'])],
                tags=tags
            )
            print(f"  📤 kyouhei.speedtest.upload = {metrics['upload']:.2f} Mbps")

            api.Metric.send(
                metric="kyouhei.speedtest.ping",
                points=[(timestamp, metrics['ping'])],
                tags=tags
            )
            print(f"  📤 kyouhei.speedtest.ping = {metrics['ping']:.2f} ms")

            api.Metric.send(
                metric="kyouhei.speedtest.test_count",
                points=[(timestamp, 1)],
                tags=tags,
                type='count'
            )
            print("  📤 kyouhei.speedtest.test_count = 1 (count)")
        else:
            for metric_name, value in metrics.items():
                full_metric_name = resolve_metric_name(prefix, metric_name)
                unit = "Mbps" if metric_name in ['download', 'upload'] else "ms"

                api.Metric.send(
                    metric=full_metric_name,
                    points=[(timestamp, value)],
                    tags=tags
                )
                print(f"  📤 {full_metric_name} = {value:.2f} {unit}")

            test_count_metric_name = resolve_metric_name(prefix, 'test_count')
            api.Metric.send(
                metric=test_count_metric_name,
                points=[(timestamp, 1)],
                tags=tags,
                type='count'
            )
            print(f"  📤 {test_count_metric_name} = 1 (count)")
        
        # Display tags
        print(f"\n🏷️  Tags ({len(tags)}):")
        for tag in tags:
            print(f"  - {tag}")
        
        print("\n✅ Successfully sent to Datadog")
        
        if logger:
            log_json(logger, "info", "Metrics sent to Datadog",
                    event_type="metrics_sent",
                    status="success",
                    metric_count=len(metrics) + 1,
                    tag_count=len(tags))
        
        if span:
            span.set_tag("metrics.count", len(metrics))
            span.set_tag("tags.count", len(tags))
            span.finish()
        
    except ImportError:
        error_msg = "datadog package not installed"
        print(f"❌ {error_msg}")
        print("   Install: pip install datadog")
        if logger:
            log_json(logger, "error", error_msg,
                    event_type="metrics_send_failed",
                    status="failed", error_type="import_error")
        if span:
            span.set_tag("error", True)
            span.set_tag("error.message", error_msg)
            span.finish()
        raise Exception(error_msg)
    except Exception as e:
        error_msg = f"Datadog API error: {e}"
        print(f"❌ {error_msg}")
        if logger:
            log_json(logger, "error", error_msg,
                    event_type="metrics_send_failed",
                    status="failed", error_type="api_error")
        if span:
            span.set_tag("error", True)
            span.set_tag("error.message", error_msg)
            span.finish()
        raise Exception(error_msg)


def dry_run_display(metrics, tags, prefix):
    """Dry-run mode: Display metrics and tags only"""
    print("\n" + "=" * 60)
    print("🏃 DRY-RUN MODE (Not sending to Datadog)")
    print("=" * 60)
    
    print("\n📊 Metrics:")
    for metric_name, value in metrics.items():
        full_metric_name = resolve_metric_name(prefix, metric_name)
        unit = "Mbps" if metric_name in ['download', 'upload'] else "ms"
        print(f"  {full_metric_name}: {value:.2f} {unit}")
    
    test_count_metric_name = resolve_metric_name(prefix, 'test_count')
    print(f"\n  {test_count_metric_name}: 1 (count)")
    
    print("\n🏷️  Tags:")
    for tag in tags:
        print(f"  - {tag}")
    
    print("\n" + "=" * 60)


def run_speedtest_cycle(metric_prefix, dd_api_key, dd_site, user_tags, logger, enable_traces, dry_run, server_id, country_file):
    """Run a single speedtest cycle"""
    try:
        # Re-read each cycle so the mounted file can change without a restart.
        country_code = read_country_code(country_file)
        if country_code:
            print(f"🌏 Country file: {country_file} -> {country_code}")
        speedtest_data, server_selection = run_speedtest(
            logger=logger,
            enable_tracing=enable_traces,
            server_id=server_id,
            country_code=country_code,
        )
        
        # Parse results
        metrics, server_tags = parse_speedtest_results(speedtest_data, server_selection)
        
        # Combine all tags
        all_tags = server_tags + user_tags
        
        if dry_run:
            # Dry-run mode
            dry_run_display(metrics, all_tags, metric_prefix)
        else:
            # Production mode
            send_to_datadog(metrics, all_tags, metric_prefix, dd_api_key, dd_site,
                           logger=logger, enable_tracing=enable_traces)
        
        return True
    except Exception as e:
        error_msg = f"Speedtest cycle failed: {e}"
        print(f"❌ {error_msg}")
        if logger:
            log_json(logger, "error", error_msg,
                    event_type="speedtest_cycle_failed",
                    status="failed", error_type="cycle_error")
        return False


def main():
    parser = argparse.ArgumentParser(
        description='Run internet speed test and send results to Datadog'
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Display metrics and tags only, do not send to Datadog'
    )
    parser.add_argument(
        '--once',
        action='store_true',
        help='Run once and exit (equivalent to --count 1)'
    )
    parser.add_argument(
        '--count',
        type=int,
        metavar='N',
        help='Run N times and exit (default: infinite loop)'
    )
    args = parser.parse_args()
    
    # Determine run count
    if args.once:
        max_count = 1
    elif args.count:
        max_count = args.count
    else:
        max_count = None  # Infinite loop
    
    # Setup signal handlers for graceful shutdown
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # Load configuration from environment variables
    explicit_metric_prefix = os.getenv('METRIC_PREFIX')
    metrics_test_mode = parse_boolean(os.getenv('METRICS_TEST_MODE', 'false'))
    metric_prefix = resolve_metric_prefix(explicit_metric_prefix, metrics_test_mode)
    dd_site = os.getenv('DD_SITE', 'datadoghq.com')
    dd_api_key = os.getenv('DD_API_KEY', '')
    dd_tags_str = os.getenv('DD_TAGS', '')
    enable_logs = os.getenv('ENABLE_LOGS', 'false').lower() == 'true'
    enable_traces = os.getenv('ENABLE_TRACES', 'false').lower() == 'true'
    frequency = int(os.getenv('SPEEDTEST_FREQUENCY', '300'))  # Default: 5 minutes
    server_id_str = os.getenv('SPEEDTEST_SERVER_ID', '')  # Default: auto selection
    country_file = os.getenv('SPEEDTEST_COUNTRY_FILE', DEFAULT_COUNTRY_FILE)
    
    # Setup logger (if logs enabled) before any configuration warnings are printed.
    logger = None
    if enable_logs:
        logger = setup_json_logging()
        global _ACTIVE_LOGGER
        _ACTIVE_LOGGER = logger
        enable_json_output()

    # Parse server ID
    server_id = None
    if server_id_str:
        try:
            server_id = int(server_id_str)
        except ValueError:
            print(f"⚠️  Invalid SPEEDTEST_SERVER_ID: {server_id_str}, using auto selection")
            server_id = None
    
    # Parse user-defined tags
    user_tags = [tag.strip() for tag in dd_tags_str.split(',') if tag.strip()]
    
    # Check tracing status
    tracing_status = "Disabled"
    if enable_traces:
        if DDTRACE_AVAILABLE:
            tracing_status = (
                f"Enabled (Agent: {os.getenv('DD_AGENT_HOST', 'localhost')}:8126)"
            )
            # Set service name and env
            if tracer:
                tracer.set_tags({
                    "env": os.getenv("DD_ENV", "dev"),
                    "service": "speedtest",
                    "version": os.getenv("DD_VERSION", "unknown")
                })
        else:
            tracing_status = "Disabled (ddtrace not installed)"
            print("⚠️  ENABLE_TRACES=true but ddtrace is not installed")
            print("   Install: pip install ddtrace")
    
    # Display configuration
    if max_count is None:
        run_mode = f"Infinite loop (every {frequency}s)"
    elif max_count == 1:
        run_mode = "Once"
    else:
        run_mode = f"{max_count} times (every {frequency}s)"
    
    print(f"⚙️  Configuration:")
    print(f"   Metric Prefix: {metric_prefix}")
    print(f"   Metrics Test Mode: {metrics_test_mode}")
    print(f"   DD Site: {dd_site}")
    print(f"   User Tags: {user_tags if user_tags else '(none)'}")
    print(f"   Server ID: {server_id if server_id else 'auto (used when the country file is empty)'}")
    print(f"   Country file: {country_file} (read before each test)")
    print(f"   Logs: {'Enabled (JSON output)' if enable_logs else 'Disabled'}")
    print(f"   Traces: {tracing_status}")
    print(f"   Frequency: {frequency}s ({frequency // 60} minutes)")
    print(f"   Run Mode: {run_mode}")
    print(f"   Dry-run: {args.dry_run}")
    print()
    
    if not args.dry_run and not dd_api_key:
        print("❌ DD_API_KEY is not set")
        print("   Create .env file or set environment variable")
        if logger:
            log_json(logger, "error", "DD_API_KEY not set",
                    event_type="configuration_invalid",
                    status="failed")
        sys.exit(1)
    
    if logger:
        log_json(logger, "info", "Speedtest service started",
                event_type="service_started",
                status="initialized",
                metric_prefix=metric_prefix,
                logs_enabled=enable_logs,
                traces_enabled=enable_traces and DDTRACE_AVAILABLE,
                frequency=frequency,
                max_count=max_count)
    
    # Run loop with fixed schedule
    test_count = 0
    skipped_count = 0
    start_time = datetime.now()
    next_run_time = start_time
    is_running = False
    
    while not shutdown_flag:
        current_time = datetime.now()
        
        # Check if it's time to run
        if current_time >= next_run_time:
            # Check if previous test is still running
            if is_running:
                skipped_count += 1
                print(f"⚠️  Previous test still running, skipping this cycle (skipped: {skipped_count})")
                if logger:
                    log_json(logger, "warning", "Skipped cycle due to long-running test",
                            event_type="speedtest_cycle_skipped",
                            status="skipped", skipped_count=skipped_count)
                # Schedule next run
                next_run_time += timedelta(seconds=frequency)
                continue
            
            test_count += 1
            is_running = True
            
            # Display test number
            if max_count is None:
                print(f"\n{'='*60}")
                print(f"🔄 Test #{test_count}")
                print(f"{'='*60}\n")
            elif max_count > 1:
                print(f"\n{'='*60}")
                print(f"🔄 Test #{test_count} of {max_count}")
                print(f"{'='*60}\n")
            
            # Run speedtest cycle
            test_start = datetime.now()
            success = run_speedtest_cycle(
                metric_prefix=metric_prefix,
                dd_api_key=dd_api_key,
                dd_site=dd_site,
                user_tags=user_tags,
                logger=logger,
                enable_traces=enable_traces,
                dry_run=args.dry_run,
                server_id=server_id,
                country_file=country_file,
            )
            test_duration = (datetime.now() - test_start).total_seconds()
            is_running = False
            
            # Warn if test took longer than frequency
            if test_duration > frequency:
                print(f"⚠️  Test took {test_duration:.1f}s (longer than frequency {frequency}s)")
                if logger:
                    log_json(logger, "warning", "Test duration exceeds frequency",
                            event_type="speedtest_duration_exceeded",
                            test_duration=test_duration, frequency=frequency)
            
            # Check if we've reached max count
            if max_count is not None and test_count >= max_count:
                if logger:
                    log_json(logger, "info", "Reached max count, exiting",
                            event_type="service_completed",
                            status="completed", test_count=test_count, 
                            max_count=max_count, skipped_count=skipped_count)
                break
            
            # Calculate next run time (fixed schedule from start)
            next_run_time = start_time + timedelta(seconds=frequency * test_count)
            
            # If next run is in the past, adjust to next interval from now
            if next_run_time <= datetime.now():
                missed = int((datetime.now() - next_run_time).total_seconds() / frequency) + 1
                next_run_time += timedelta(seconds=frequency * missed)
                if missed > 0:
                    print(f"⚠️  Behind schedule, adjusted next run time")
        
        # Display next run time and sleep
        if not shutdown_flag and (max_count is None or test_count < max_count):
            time_until_next = (next_run_time - datetime.now()).total_seconds()
            if time_until_next > 0:
                print(f"\n⏰ Next test at: {next_run_time.strftime('%Y-%m-%d %H:%M:%S')} (in {int(time_until_next)}s)")
                print(f"   Press Ctrl+C to stop")
                
                # Sleep in small intervals to allow quick shutdown
                sleep_interval = min(5, max(1, time_until_next))
                while (datetime.now() < next_run_time) and not shutdown_flag:
                    time.sleep(min(sleep_interval, (next_run_time - datetime.now()).total_seconds()))
    
    # Shutdown
    if logger:
        log_json(logger, "info", "Speedtest service stopped",
                event_type="service_stopped",
                status="shutdown", total_tests=test_count, skipped_tests=skipped_count)
    print(f"\n✅ Service stopped")
    print(f"   Total tests run: {test_count}")
    if skipped_count > 0:
        print(f"   Tests skipped: {skipped_count}")
    sys.exit(0)


if __name__ == '__main__':
    main()

