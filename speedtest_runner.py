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
    log_entry = {
        "timestamp": datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
        "service": "speedtest",
        "message": message,
        **extra_fields
    }
    # stdoutにJSON出力（Datadog Agentがあれば収集される）
    print(json.dumps(log_entry), file=sys.stdout)


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


def run_speedtest(logger=None, enable_tracing=False, server_id=None):
    """Run speedtest-cli and return results as JSON"""
    
    # Start tracing span
    span = None
    if enable_tracing and DDTRACE_AVAILABLE and tracer:
        span = tracer.trace("speedtest.run", service="speedtest")
        if server_id:
            span.set_tag("speedtest.server_id_requested", server_id)
    
    # Build command
    cmd = ['speedtest-cli', '--json']
    server_selection = 'auto'
    
    if server_id:
        cmd.extend(['--server', str(server_id)])
        server_selection = 'manual'
        print(f"🚀 Running speed test (server ID: {server_id})...")
        if logger:
            log_json(logger, "info", "Starting speedtest", status="running", 
                    server_id=server_id, server_selection="manual")
    else:
        print("🚀 Running speed test (auto server selection)...")
        if logger:
            log_json(logger, "info", "Starting speedtest", status="running",
                    server_selection="auto")
    
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
            timeout=120
        )
        data = json.loads(result.stdout)
        print("✅ Speed test completed")
        
        if logger:
            log_json(logger, "info", "Speedtest completed", 
                    status="success",
                    download_mbps=round(data['download'] / 1_000_000, 2),
                    upload_mbps=round(data['upload'] / 1_000_000, 2),
                    ping_ms=round(data['ping'], 2),
                    server_selection=server_selection)
        
        if span:
            span.set_tag("speedtest.download_mbps", round(data['download'] / 1_000_000, 2))
            span.set_tag("speedtest.upload_mbps", round(data['upload'] / 1_000_000, 2))
            span.set_tag("speedtest.ping_ms", round(data['ping'], 2))
            span.set_tag("speedtest.server_selection", server_selection)
            span.finish()
        
        return data, server_selection
        
    except (subprocess.CalledProcessError, json.JSONDecodeError) as e:
        # If manual server failed, try auto selection as fallback
        if server_id:
            error_msg = f"Speed test with server {server_id} failed, falling back to auto selection"
            print(f"⚠️  {error_msg}")
            if logger:
                log_json(logger, "warning", error_msg, 
                        status="fallback", 
                        server_id=server_id,
                        error=str(e))
            if span:
                span.set_tag("speedtest.fallback", True)
            
            # Retry with auto selection
            return run_speedtest(logger=logger, enable_tracing=False, server_id=None)
        
        # If auto selection also failed, raise error
        error_msg = f"Speed test failed: {str(e)}"
        print(f"❌ {error_msg}")
        if logger:
            log_json(logger, "error", error_msg, status="failed", error_type="process_error")
        if span:
            span.set_tag("error", True)
            span.set_tag("error.message", error_msg)
            span.finish()
        sys.exit(1)
        
    except subprocess.TimeoutExpired:
        error_msg = "Speed test timeout"
        print(f"❌ {error_msg}")
        if logger:
            log_json(logger, "error", error_msg, status="timeout", error_type="timeout")
        if span:
            span.set_tag("error", True)
            span.set_tag("error.message", error_msg)
            span.finish()
        sys.exit(1)
    except FileNotFoundError:
        error_msg = "speedtest-cli not installed"
        print(f"❌ {error_msg}")
        print("   Install: pip install speedtest-cli")
        if logger:
            log_json(logger, "error", error_msg, status="failed", error_type="not_found")
        if span:
            span.set_tag("error", True)
            span.set_tag("error.message", error_msg)
            span.finish()
        sys.exit(1)


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
        for metric_name, value in metrics.items():
            full_metric_name = f"{prefix}.speedtest.{metric_name}"
            unit = "Mbps" if metric_name in ['download', 'upload'] else "ms"
            
            api.Metric.send(
                metric=full_metric_name,
                points=[(timestamp, value)],
                tags=tags
            )
            print(f"  📤 {full_metric_name} = {value:.2f} {unit}")
        
        # Test execution counter
        api.Metric.send(
            metric=f"{prefix}.speedtest.test_count",
            points=[(timestamp, 1)],
            tags=tags,
            type='count'
        )
        print(f"  📤 {prefix}.speedtest.test_count = 1 (count)")
        
        # Display tags
        print(f"\n🏷️  Tags ({len(tags)}):")
        for tag in tags:
            print(f"  - {tag}")
        
        print("\n✅ Successfully sent to Datadog")
        
        if logger:
            log_json(logger, "info", "Metrics sent to Datadog",
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
            log_json(logger, "error", error_msg, status="failed", error_type="import_error")
        if span:
            span.set_tag("error", True)
            span.set_tag("error.message", error_msg)
            span.finish()
        sys.exit(1)
    except Exception as e:
        error_msg = f"Datadog API error: {e}"
        print(f"❌ {error_msg}")
        if logger:
            log_json(logger, "error", error_msg, status="failed", error_type="api_error")
        if span:
            span.set_tag("error", True)
            span.set_tag("error.message", error_msg)
            span.finish()
        sys.exit(1)


def dry_run_display(metrics, tags, prefix):
    """Dry-run mode: Display metrics and tags only"""
    print("\n" + "=" * 60)
    print("🏃 DRY-RUN MODE (Not sending to Datadog)")
    print("=" * 60)
    
    print("\n📊 Metrics:")
    for metric_name, value in metrics.items():
        full_metric_name = f"{prefix}.speedtest.{metric_name}"
        unit = "Mbps" if metric_name in ['download', 'upload'] else "ms"
        print(f"  {full_metric_name}: {value:.2f} {unit}")
    
    print(f"\n  {prefix}.speedtest.test_count: 1 (count)")
    
    print("\n🏷️  Tags:")
    for tag in tags:
        print(f"  - {tag}")
    
    print("\n" + "=" * 60)


def run_speedtest_cycle(metric_prefix, dd_api_key, dd_site, user_tags, logger, enable_traces, dry_run, server_id):
    """Run a single speedtest cycle"""
    try:
        # Run speedtest
        speedtest_data, server_selection = run_speedtest(logger=logger, enable_tracing=enable_traces, server_id=server_id)
        
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
            log_json(logger, "error", error_msg, status="failed", error_type="cycle_error")
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
    metric_prefix = os.getenv('METRIC_PREFIX', 'test.kyouhei')
    dd_site = os.getenv('DD_SITE', 'datadoghq.com')
    dd_api_key = os.getenv('DD_API_KEY', '')
    dd_tags_str = os.getenv('DD_TAGS', '')
    enable_logs = os.getenv('ENABLE_LOGS', 'false').lower() == 'true'
    enable_traces = os.getenv('ENABLE_TRACES', 'false').lower() == 'true'
    frequency = int(os.getenv('SPEEDTEST_FREQUENCY', '300'))  # Default: 5 minutes
    server_id_str = os.getenv('SPEEDTEST_SERVER_ID', '')  # Default: auto selection
    
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
    
    # Setup logger (if logs enabled)
    logger = None
    if enable_logs:
        logger = setup_json_logging()
    
    # Check tracing status
    tracing_status = "Disabled"
    if enable_traces:
        if DDTRACE_AVAILABLE:
            tracing_status = "Enabled (Agent: localhost:8126)"
            # Set service name and env
            if tracer:
                tracer.set_tags({
                    "env": os.getenv("DD_ENV", "dev"),
                    "service": "speedtest",
                    "version": os.getenv("DD_VERSION", "1.0.0")
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
    print(f"   DD Site: {dd_site}")
    print(f"   User Tags: {user_tags if user_tags else '(none)'}")
    print(f"   Server ID: {server_id if server_id else 'auto'}")
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
            log_json(logger, "error", "DD_API_KEY not set", status="failed")
        sys.exit(1)
    
    if logger:
        log_json(logger, "info", "Speedtest service started",
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
                server_id=server_id
            )
            test_duration = (datetime.now() - test_start).total_seconds()
            is_running = False
            
            # Warn if test took longer than frequency
            if test_duration > frequency:
                print(f"⚠️  Test took {test_duration:.1f}s (longer than frequency {frequency}s)")
                if logger:
                    log_json(logger, "warning", "Test duration exceeds frequency",
                            test_duration=test_duration, frequency=frequency)
            
            # Check if we've reached max count
            if max_count is not None and test_count >= max_count:
                if logger:
                    log_json(logger, "info", "Reached max count, exiting",
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
                status="shutdown", total_tests=test_count, skipped_tests=skipped_count)
    print(f"\n✅ Service stopped")
    print(f"   Total tests run: {test_count}")
    if skipped_count > 0:
        print(f"   Tests skipped: {skipped_count}")
    sys.exit(0)


if __name__ == '__main__':
    main()

