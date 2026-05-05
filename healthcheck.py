"""
healthcheck.py
Lightweight health monitor for the Polymarket hybrid bot.

Checks:
  1. Log file freshness — bot is writing logs (not silently hung)
  2. Polymarket REST API reachability
  3. Spot feed subsystem liveness (T13 heartbeat events in logs)
  4. Memory usage vs available RAM
  5. Daily PnL / kill-switch status from log scan

Usage:
    python healthcheck.py              # print status to stdout
    python healthcheck.py --alert      # also send Telegram/Discord alert if unhealthy
    python healthcheck.py --json       # output JSON (for external monitoring)

Exit codes:
    0 = healthy
    1 = degraded (some checks failed, bot still running)
    2 = critical (bot likely down or kill-switch active)

Can be called from a cron job every 5 minutes:
    */5 * * * * /usr/bin/python3 /opt/polymarket_hybrid_bot/healthcheck.py --alert >> /var/log/bot_health.log 2>&1
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

POLYMARKET_HOST = os.environ.get("BOT_POLYMARKET_HOST", "https://clob.polymarket.com")
LOG_FRESHNESS_SEC = 120        # warn if no log activity in 2 minutes
SPOT_FEED_HEARTBEAT_FRESHNESS_SEC = 120  # heartbeats fire every 60s; 2x slack
MAX_MEMORY_PCT = 85            # warn if container using > 85% of available RAM
DOCKER_CONTAINER = "polymarket-hybrid-bot"


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class CheckResult:
    name: str
    ok: bool
    message: str
    critical: bool = False   # if True and not ok → exit code 2


@dataclass
class HealthReport:
    ts: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    checks: list[CheckResult] = field(default_factory=list)

    @property
    def healthy(self) -> bool:
        return all(c.ok for c in self.checks)

    @property
    def critical(self) -> bool:
        return any(not c.ok and c.critical for c in self.checks)

    def exit_code(self) -> int:
        if self.critical:
            return 2
        if not self.healthy:
            return 1
        return 0

    def summary(self) -> str:
        lines = [f"[{self.ts}] Health: {'✅ OK' if self.healthy else '❌ DEGRADED'}"]
        for c in self.checks:
            icon = "✅" if c.ok else ("🚨" if c.critical else "⚠️")
            lines.append(f"  {icon} {c.name}: {c.message}")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "ts": self.ts,
            "healthy": self.healthy,
            "critical": self.critical,
            "checks": [
                {"name": c.name, "ok": c.ok, "message": c.message, "critical": c.critical}
                for c in self.checks
            ],
        }


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

async def check_docker_container() -> CheckResult:
    """Verify the bot container is running."""
    try:
        result = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.Status}}", DOCKER_CONTAINER],
            capture_output=True, text=True, timeout=5
        )
        status = result.stdout.strip()
        ok = status == "running"
        return CheckResult(
            name="docker_container",
            ok=ok,
            message=f"status={status}",
            critical=True,
        )
    except FileNotFoundError:
        # Docker not available — running outside container, skip check
        return CheckResult(name="docker_container", ok=True, message="docker_not_available_skipped")
    except Exception as exc:
        return CheckResult(name="docker_container", ok=False, message=str(exc), critical=True)


async def check_log_freshness() -> CheckResult:
    """Check that journald or Docker logs show recent activity."""
    try:
        result = subprocess.run(
            ["docker", "logs", "--tail", "5", "--timestamps", DOCKER_CONTAINER],
            capture_output=True, text=True, timeout=10
        )
        output = (result.stdout + result.stderr).strip()
        if not output:
            return CheckResult(
                name="log_freshness",
                ok=False,
                message="no recent log output",
                critical=True,
            )
        # Parse timestamp from last log line
        # Docker format varies: 2025-01-15T10:00:00.000Z or 2025-01-15T10:00:00.000000000Z
        last_line = output.splitlines()[-1]
        try:
            ts_str = last_line.split(" ")[0]
            # Truncate sub-second precision to 6 digits max (fromisoformat limit)
            import re as _re
            ts_str = _re.sub(r'(\.\d{6})\d+', r'\1', ts_str).replace("Z", "+00:00")
            ts = datetime.fromisoformat(ts_str)
            age_sec = (datetime.now(timezone.utc) - ts).total_seconds()
            ok = age_sec < LOG_FRESHNESS_SEC
            return CheckResult(
                name="log_freshness",
                ok=ok,
                message=f"last_log={round(age_sec)}s ago (limit={LOG_FRESHNESS_SEC}s)",
                critical=not ok,
            )
        except Exception:
            return CheckResult(name="log_freshness", ok=True, message="timestamp_parse_skipped")
    except FileNotFoundError:
        return CheckResult(name="log_freshness", ok=True, message="docker_not_available_skipped")
    except Exception as exc:
        return CheckResult(name="log_freshness", ok=False, message=str(exc))


async def check_polymarket_api() -> CheckResult:
    """Verify Polymarket CLOB REST API is reachable."""
    try:
        import aiohttp
        url = f"{POLYMARKET_HOST}/markets?limit=1"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                ok = resp.status < 500
                return CheckResult(
                    name="polymarket_api",
                    ok=ok,
                    message=f"status={resp.status}",
                    critical=False,  # outage is external, not our fault
                )
    except Exception as exc:
        return CheckResult(
            name="polymarket_api",
            ok=False,
            message=f"unreachable: {exc}",
            critical=False,
        )


async def check_spot_feed() -> CheckResult:
    """Check the spot feed subsystem is alive via T13 heartbeat events.

    Looks for at least one spot_feed_heartbeat event from any venue in
    the last SPOT_FEED_HEARTBEAT_FRESHNESS_SEC seconds. Disabled venues
    (e.g. binance via BOT_DISABLE_BINANCE_FEED — T11) intentionally do
    not emit heartbeats; this is healthy as long as at least one venue
    is firing.
    """
    try:
        result = subprocess.run(
            ["docker", "logs", "--since", "3m", DOCKER_CONTAINER],
            capture_output=True, text=True, timeout=10
        )
        output = (result.stdout + result.stderr).strip()
        if not output:
            return CheckResult(
                name="spot_feed",
                ok=False,
                message="no log output in last 3m",
                critical=False,
            )

        now = datetime.now(timezone.utc)
        latest_age_sec: float | None = None
        latest_venue: str | None = None

        # Walk lines from newest to oldest, find first spot_feed_heartbeat.
        for line in reversed(output.splitlines()):
            stripped = line.strip()
            if not stripped.startswith("{"):
                continue
            try:
                data = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if data.get("event") != "spot_feed_heartbeat":
                continue
            ts_str = data.get("timestamp", "")
            if not ts_str:
                continue
            try:
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            except ValueError:
                continue
            latest_age_sec = (now - ts).total_seconds()
            latest_venue = data.get("venue", "unknown")
            break

        if latest_age_sec is None:
            return CheckResult(
                name="spot_feed",
                ok=False,
                message="no spot_feed_heartbeat in last 3m",
                critical=False,
            )

        ok = latest_age_sec < SPOT_FEED_HEARTBEAT_FRESHNESS_SEC
        return CheckResult(
            name="spot_feed",
            ok=ok,
            message=(
                f"last_heartbeat={round(latest_age_sec)}s ago "
                f"venue={latest_venue} "
                f"(limit={SPOT_FEED_HEARTBEAT_FRESHNESS_SEC}s)"
            ),
            critical=not ok,
        )
    except FileNotFoundError:
        return CheckResult(name="spot_feed", ok=True, message="docker_not_available_skipped")
    except Exception as exc:
        return CheckResult(name="spot_feed", ok=False, message=str(exc))


async def check_memory() -> CheckResult:
    """Check container memory usage vs system RAM."""
    try:
        result = subprocess.run(
            ["docker", "stats", "--no-stream", "--format",
             "{{.MemUsage}}\t{{.MemPerc}}", DOCKER_CONTAINER],
            capture_output=True, text=True, timeout=10
        )
        line = result.stdout.strip()
        if not line:
            return CheckResult(name="memory", ok=True, message="stats_unavailable")

        usage, perc_str = line.split("\t")
        perc = float(perc_str.replace("%", "").strip())
        ok = perc < MAX_MEMORY_PCT
        return CheckResult(
            name="memory",
            ok=ok,
            message=f"usage={usage.strip()} ({perc:.1f}%) limit={MAX_MEMORY_PCT}%",
            critical=False,
        )
    except FileNotFoundError:
        return CheckResult(name="memory", ok=True, message="docker_not_available_skipped")
    except Exception as exc:
        return CheckResult(name="memory", ok=False, message=str(exc))


async def check_kill_switch() -> CheckResult:
    """Scan recent logs for kill-switch events."""
    try:
        result = subprocess.run(
            ["docker", "logs", "--tail", "500", DOCKER_CONTAINER],
            capture_output=True, text=True, timeout=10
        )
        output = result.stdout + result.stderr
        kill_lines = [l for l in output.splitlines() if "kill_switch_triggered" in l]
        if kill_lines:
            last = kill_lines[-1]
            # Try to extract drawdown from JSON log
            try:
                data = json.loads(last.split("] ")[-1])
                dd = data.get("drawdown_pct", "?")
                msg = f"TRIGGERED — drawdown={dd}"
            except Exception:
                msg = "TRIGGERED — check logs"
            return CheckResult(
                name="kill_switch",
                ok=False,
                message=msg,
                critical=True,
            )
        return CheckResult(name="kill_switch", ok=True, message="not_triggered")
    except FileNotFoundError:
        return CheckResult(name="kill_switch", ok=True, message="docker_not_available_skipped")
    except Exception as exc:
        return CheckResult(name="kill_switch", ok=False, message=str(exc))


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

async def run_healthcheck() -> HealthReport:
    report = HealthReport()
    checks = await asyncio.gather(
        check_docker_container(),
        check_log_freshness(),
        check_polymarket_api(),
        check_spot_feed(),
        check_memory(),
        check_kill_switch(),
        return_exceptions=False,
    )
    report.checks = list(checks)
    return report


async def main(alert: bool, as_json: bool) -> int:
    report = await run_healthcheck()

    if as_json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(report.summary())

    if alert and not report.healthy:
        try:
            from utils.alerts import send_alert
            level = "critical" if report.critical else "warning"
            failing = [c for c in report.checks if not c.ok]
            msg = "Bot health check FAILED:\n" + "\n".join(
                f"  • {c.name}: {c.message}" for c in failing
            )
            await send_alert(msg, level=level)
        except Exception as exc:
            print(f"Alert send failed: {exc}", file=sys.stderr)

    return report.exit_code()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Polymarket bot health check")
    parser.add_argument("--alert", action="store_true", help="Send alert if unhealthy")
    parser.add_argument("--json", action="store_true", dest="as_json", help="JSON output")
    args = parser.parse_args()

    exit_code = asyncio.run(main(args.alert, args.as_json))
    sys.exit(exit_code)

