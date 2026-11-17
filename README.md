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

The default production prefix is `kyouhei`, so the production metric names are
`kyouhei.speedtest.download`, `kyouhei.speedtest.upload`, `kyouhei.speedtest.ping`,
and `kyouhei.speedtest.test_count`. Set `METRICS_TEST_MODE=true` to use the
`test.kyouhei` prefix when `METRIC_PREFIX` is not set. A custom `METRIC_PREFIX`
remains supported and is followed by `.speedtest` (for example,
`METRIC_PREFIX=customer.speedtest` produces `customer.speedtest.speedtest.*`).
The production metric names are written as literals at the Datadog send call
sites so Metrics Catalog source-code detection can link them to this repository.
Test and custom-prefix names are generated dynamically and are not intended for
that catalog linkage.

### Tags (automatically added)

- `host`: Hostname of the machine running the test
- `speedtest_server_name`: Test server name
- `speedtest_server_country`: Test server country
- `speedtest_server_sponsor`: Test server sponsor/ISP
- `speedtest_server_id`: Test server ID
- `server_selection`: `manual`, `auto`, or `country` (how the server was selected)
- `isp`: Your ISP name
- `container_id`: Full container ID (64 chars, Docker only)
- `container_name`: Container name (Docker only)
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

**Using pre-built multi-architecture image (amd64, arm64):**

```bash
# 1. Create .env file
cp .env.sample .env
# Edit .env and set:
#   - DD_API_KEY (required)
#   - HOST_HOSTNAME (recommended for identifying servers)

# 2. Run with docker-compose (uses ghcr.io/kyoppe/speedtest-datadog:latest)
docker-compose up -d

# 3. Check logs
docker-compose logs -f speedtest

# 4. Stop
docker-compose down
```

**Building locally:**

```bash
# 1. Build the image
docker build -t speedtest-datadog .

# 2. Update docker-compose.yaml to use local image
# Uncomment 'build: .' and comment out 'image: ghcr.io/...'

# 3. Run with docker-compose
docker-compose up -d
```

## Configuration

### Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `DD_API_KEY` | ✅ Yes | - | Datadog API Key |
| `DD_SITE` | No | `datadoghq.com` | Datadog site (e.g., `datadoghq.eu`) |
| `METRIC_PREFIX` | No | `kyouhei` | Custom metric prefix. When set, it takes precedence over `METRICS_TEST_MODE` and is followed by `.speedtest` |
| `METRICS_TEST_MODE` | No | `false` | When `true` and `METRIC_PREFIX` is unset, use `test.kyouhei.speedtest.*` |
| `DD_TAGS` | No | - | Comma-separated tags (e.g., `env:prod,team:ops`) |
| `SPEEDTEST_FREQUENCY` | No | `300` | Test interval in seconds (300 = 5 minutes) |
| `SPEEDTEST_COUNTRY_FILE` | No | `/config/speedtest-country` | File with one country code (`JP`) or country name (`Japan`). Read before every test. Servers in that country are tried nearest-first. A failure tries the next server in the same country. |
| `SPEEDTEST_SERVER_ID` | No | - | Server ID used only when the country file is missing or blank (e.g., `7139` for Tsukuba). If that server is unavailable, automatic selection is used. |
| `HOST_HOSTNAME` | No | auto | **Important for Docker**: Set to actual hostname to differentiate metrics between multiple servers. Run `hostname` to get value. |
| `ENABLE_LOGS` | No | `false` | Enable JSON structured logs to stdout |
| `ENABLE_TRACES` | No | `false` | Enable APM traces (requires Datadog Agent) |
| `DD_AGENT_HOST` | No | - | Datadog Agent hostname for APM traces (for example, `datadog-agent` in Docker Compose) |
| `DD_ENV` | No | - | Environment tag for traces |
| `DD_VERSION` | No | Image version | Version attached to traces and structured logs |

When `ENABLE_LOGS=true`, every stdout line is emitted as one JSON log event.
Each event includes `event_type` and `dd.version`; logs written while a trace
span is active also include `dd.trace_id` and `dd.span_id`.

### Country file

Put a country code or country name in the file named by `SPEEDTEST_COUNTRY_FILE`. The process reads it before every test, so changing the file does not require a restart.

```text
JP
```

Mount it in Docker:

```yaml
services:
  speedtest:
    image: ghcr.io/kyoppe/speedtest-datadog:latest
    volumes:
      - ./speedtest-country:/config/speedtest-country:ro
```

`server_selection:country` marks a test that used this filter. `speedtest_server_id` and `speedtest_server_name` still identify the server that answered. A blank or missing file leaves the previous server-ID and automatic selection behavior in place.

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

This project provides a pre-built multi-architecture image at:
- **`ghcr.io/kyoppe/speedtest-datadog:latest`**
- Supports: `linux/amd64` (Intel/AMD), `linux/arm64` (Apple Silicon, modern ARM)

### Using Pre-built Image

```bash
# Pull and run (automatically selects correct architecture)
docker pull ghcr.io/kyoppe/speedtest-datadog:latest
```

The `docker-compose.yaml` file is already configured to use this image.

### Publishing Your Own Image

#### GitHub Container Registry (ghcr.io)

```bash
# 1. Login to GitHub Container Registry
echo $GITHUB_TOKEN | docker login ghcr.io -u YOUR_GITHUB_USERNAME --password-stdin

# 2. Build multi-architecture image
VERSION="$(git rev-parse --short HEAD)"
docker buildx build --platform linux/amd64,linux/arm64 \
  --build-arg DD_VERSION="$VERSION" \
  -t ghcr.io/YOUR_GITHUB_USERNAME/speedtest-datadog:"$VERSION" \
  -t ghcr.io/YOUR_GITHUB_USERNAME/speedtest-datadog:latest \
  --push .

# 3. Make image public (optional)
# Go to: https://github.com/YOUR_GITHUB_USERNAME?tab=packages
# Click on 'speedtest-datadog' → Package settings → Change visibility → Public
```

`DD_VERSION` is embedded in the image and exposed to the container. Use the
short Git commit hash for both `DD_VERSION` and the version tag when publishing
a release.

**Note**: Replace `YOUR_GITHUB_USERNAME` with your actual GitHub username.

#### Creating GitHub Personal Access Token (PAT)

1. Go to GitHub Settings → Developer settings → Personal access tokens → Tokens (classic)
2. Generate new token with these scopes:
   - `write:packages`
   - `read:packages`
   - `delete:packages`

## Usage Examples

### Standalone Container

```bash
docker run -d \
  --name speedtest \
  -e DD_API_KEY=your_api_key \
  -e DD_SITE=datadoghq.com \
  -e DD_TAGS=env:home,location:tokyo \
  -e SPEEDTEST_FREQUENCY=300 \
  -e HOST_HOSTNAME=$(hostname) \
  --restart unless-stopped \
  ghcr.io/kyoppe/speedtest-datadog:latest
```

### Add to Existing docker-compose.yaml

```yaml
services:
  speedtest:
    image: ghcr.io/kyoppe/speedtest-datadog:latest
    container_name: speedtest
    environment:
      - DD_API_KEY=${DD_API_KEY}
      - DD_SITE=datadoghq.com
      - DD_TAGS=env:production,location:home
      - SPEEDTEST_FREQUENCY=300
      - HOST_HOSTNAME=${HOST_HOSTNAME}  # Set in .env file
      - ENABLE_LOGS=true
      - ENABLE_TRACES=true
      - DD_AGENT_HOST=datadog-agent
    volumes:
      - ./speedtest-country:/config/speedtest-country:ro
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
2. Search for: `kyouhei.speedtest.*` (or `test.kyouhei.speedtest.*` in test mode)
3. Create a dashboard to visualize:
   - Download/upload speeds over time
   - Ping latency trends
   - Test execution count

## Development

### Project Structure

```
speedtest-datadog/
├── speedtest_runner.py    # Main application
├── speedtest_runner_test.py # Unit tests
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

