# Speedtest to Datadog

A Docker container that periodically runs internet speed tests and sends the results as metrics to Datadog.

## Features

- 📊 **Metrics**: Download speed, upload speed, ping, test count
- 📝 **Logs**: Structured JSON logs (optional, requires Datadog Agent)
- 🔍 **Traces**: APM tracing (optional, requires Datadog Agent)
- 🏷️ **Tags**: Automatic server info tags + custom user tags
- ⏰ **Scheduled**: Fixed interval execution with overlap protection
- 🐳 **Containerized**: Easy deployment with Docker

## Metrics Sent

| Metric Name | Type | Unit | Description |
|------------|------|------|-------------|
| `{prefix}.speedtest.download` | gauge | Mbps | Download speed |
| `{prefix}.speedtest.upload` | gauge | Mbps | Upload speed |
| `{prefix}.speedtest.ping` | gauge | ms | Ping latency |
| `{prefix}.speedtest.test_count` | count | - | Number of tests executed |

### Tags (automatically added)

- `speedtest_server_name`: Test server name
- `speedtest_server_country`: Test server country
- `speedtest_server_sponsor`: Test server sponsor/ISP
- `speedtest_server_id`: Test server ID
- `isp`: Your ISP name
- Plus any custom tags you define in `DD_TAGS`

## Quick Start

### Local Testing (without Docker)

```bash
# 1. Create virtual environment
python3 -m venv venv
source venv/bin/activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Create .env file
cp .env.sample .env
# Edit .env and set your DD_API_KEY

# 4. Test with dry-run (doesn't send to Datadog)
python speedtest_runner.py --dry-run

# 5. Run once
python speedtest_runner.py --once

# 6. Run in loop (default: every 5 minutes)
python speedtest_runner.py
```

### Docker (Recommended)

```bash
# 1. Build the image
docker build -t speedtest-datadog .

# 2. Run with docker-compose
cp .env.sample .env
# Edit .env and set your DD_API_KEY
docker-compose up -d

# 3. Check logs
docker-compose logs -f speedtest

# 4. Stop
docker-compose down
```

## Configuration

### Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `DD_API_KEY` | ✅ Yes | - | Datadog API Key |
| `DD_SITE` | No | `datadoghq.com` | Datadog site (e.g., `datadoghq.eu`) |
| `METRIC_PREFIX` | No | `test.kyouhei` | Metric name prefix |
| `DD_TAGS` | No | - | Comma-separated tags (e.g., `env:prod,team:ops`) |
| `SPEEDTEST_FREQUENCY` | No | `300` | Test interval in seconds (300 = 5 minutes) |
| `ENABLE_LOGS` | No | `false` | Enable JSON structured logs to stdout |
| `ENABLE_TRACES` | No | `false` | Enable APM traces (requires Datadog Agent) |
| `DD_ENV` | No | `production` | Environment tag for traces |

### Command Line Options

```bash
# Run once and exit
python speedtest_runner.py --once

# Run N times and exit
python speedtest_runner.py --count 10

# Dry-run mode (display only, don't send)
python speedtest_runner.py --dry-run

# Combine options
python speedtest_runner.py --dry-run --count 3
```

## Docker Image Publishing

### GitHub Container Registry (ghcr.io)

```bash
# 1. Login to GitHub Container Registry
echo $GITHUB_TOKEN | docker login ghcr.io -u YOUR_GITHUB_USERNAME --password-stdin

# 2. Build with proper tag
docker build -t ghcr.io/YOUR_GITHUB_USERNAME/speedtest-datadog:latest .

# 3. Push to registry
docker push ghcr.io/YOUR_GITHUB_USERNAME/speedtest-datadog:latest

# 4. Use in other machines
docker pull ghcr.io/YOUR_GITHUB_USERNAME/speedtest-datadog:latest
```

**Note**: Replace `YOUR_GITHUB_USERNAME` with your actual GitHub username.

To create a GitHub Personal Access Token (PAT):
1. Go to GitHub Settings → Developer settings → Personal access tokens → Tokens (classic)
2. Generate new token with these scopes:
   - `write:packages`
   - `read:packages`
   - `delete:packages`

### Make Image Public

After pushing, go to:
- `https://github.com/YOUR_GITHUB_USERNAME?tab=packages`
- Click on `speedtest-datadog`
- Package settings → Change visibility → Public

## Usage Examples

### Standalone Container

```bash
docker run -d \
  --name speedtest \
  -e DD_API_KEY=your_api_key \
  -e DD_SITE=datadoghq.com \
  -e METRIC_PREFIX=kyouhei \
  -e DD_TAGS=env:home,location:tokyo \
  -e SPEEDTEST_FREQUENCY=300 \
  --restart unless-stopped \
  ghcr.io/YOUR_GITHUB_USERNAME/speedtest-datadog:latest
```

### Add to Existing docker-compose.yaml

```yaml
services:
  speedtest:
    image: ghcr.io/YOUR_GITHUB_USERNAME/speedtest-datadog:latest
    container_name: speedtest
    environment:
      - DD_API_KEY=${DD_API_KEY}
      - DD_SITE=datadoghq.com
      - METRIC_PREFIX=kyouhei
      - DD_TAGS=env:production,location:home
      - SPEEDTEST_FREQUENCY=300
    restart: unless-stopped
```

### With Datadog Agent (Full Observability)

Uncomment the `datadog-agent` service in `docker-compose.yaml` and set:

```yaml
services:
  speedtest:
    environment:
      - ENABLE_LOGS=true
      - ENABLE_TRACES=true
```

This enables:
- **Metrics**: Always sent via HTTP API ✓
- **Logs**: JSON logs collected by Agent ✓
- **Traces**: APM traces sent to Agent ✓

## Scheduling Behavior

The service uses a **fixed schedule** approach:

- Tests run at exact intervals from the start time
- Example: Start at 10:00:00, frequency 300s (5 min)
  - Test 1: 10:00:00
  - Test 2: 10:05:00 (exactly 5 minutes later)
  - Test 3: 10:10:00

If a test takes longer than the frequency:
- That cycle is **skipped** (no overlap)
- Warning is logged
- Next test runs at the next scheduled time

## Troubleshooting

### Container won't start

```bash
# Check logs
docker logs speedtest-datadog

# Common issues:
# - DD_API_KEY not set
# - Network connectivity issues
```

### Tests are being skipped

The interval is too short for your network speed. Increase `SPEEDTEST_FREQUENCY`:

```bash
# Change from 5 minutes to 10 minutes
SPEEDTEST_FREQUENCY=600
```

### Metrics not appearing in Datadog

1. Check API key is correct
2. Check DD_SITE matches your Datadog account
3. Look for errors in container logs
4. Test with `--dry-run` first

### Check metrics in Datadog

1. Go to Datadog → Metrics → Explorer
2. Search for: `test.kyouhei.speedtest.*` or your `METRIC_PREFIX`
3. Create a dashboard to visualize:
   - Download/upload speeds over time
   - Ping latency trends
   - Test execution count

## Development

### Project Structure

```
speedtest-datadog/
├── speedtest_runner.py    # Main application
├── requirements.txt        # Python dependencies
├── Dockerfile             # Container image definition
├── docker-compose.yaml    # Sample deployment config
├── .env.sample           # Environment variables template
├── .gitignore            # Git ignore rules
└── README.md             # This file
```

### Running Tests Locally

```bash
# Quick test with dry-run
python speedtest_runner.py --dry-run --once

# Test 3 times with 1 minute interval
SPEEDTEST_FREQUENCY=60 python speedtest_runner.py --count 3

# Enable all observability features (requires Datadog Agent)
ENABLE_LOGS=true ENABLE_TRACES=true python speedtest_runner.py --once
```

## License

MIT

## Contributing

Issues and pull requests are welcome!

