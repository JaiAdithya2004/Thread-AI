"""
In-process metrics store — no external library required.

Tracks:
    request_count, request_latency_seconds (histogram), error_count,
    llm_tokens_total (prompt + completion), llm_cost_usd_total,
    composio_calls_total, cache_hits_total, cache_misses_total,
    extraction_throughput_total, queue_depth (read from Redis on /metrics scrape)

Expose via prometheus_text() which returns Prometheus text-format output.

Thread-safe via threading.Lock for counter mutations.
Latency samples stored in a bounded deque (max 10 000); percentiles computed on demand.
"""

import math
import threading
from collections import deque

# ── Histogram bucket boundaries ────────────────────────────────────────────────
_BUCKETS = [0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0]

# ── Internal state ─────────────────────────────────────────────────────────────
_lock = threading.Lock()

# Counters — plain ints/floats
_request_count: int = 0
_error_count: int = 0
_llm_prompt_tokens: int = 0
_llm_completion_tokens: int = 0
_llm_cost_usd: float = 0.0
_composio_calls: int = 0
_cache_hits: int = 0
_cache_misses: int = 0
_extraction_total: int = 0

# Histogram: (count, sum, bucket_counts)
# bucket_counts is a list aligned with _BUCKETS plus one +Inf bucket
_lat_count: int = 0
_lat_sum: float = 0.0
_lat_buckets: list = [0] * (len(_BUCKETS) + 1)   # +Inf at index -1

# Bounded sample deque for percentile calculation
_lat_samples: deque = deque(maxlen=10_000)


# ── Public recording API ────────────────────────────────────────────────────────

def record_request(method: str, path: str, status_code: int, duration_s: float) -> None:
    """Record one HTTP request — call at the end of the request/response cycle."""
    global _request_count, _error_count, _lat_count, _lat_sum
    with _lock:
        _request_count += 1
        if status_code >= 500:
            _error_count += 1
        _lat_count += 1
        _lat_sum += duration_s
        _lat_samples.append(duration_s)
        for i, upper in enumerate(_BUCKETS):
            if duration_s <= upper:
                _lat_buckets[i] += 1
        _lat_buckets[-1] += 1   # +Inf always increments


def record_llm_usage(prompt_tokens: int, completion_tokens: int, cost_usd: float) -> None:
    """Record LLM token and cost usage."""
    global _llm_prompt_tokens, _llm_completion_tokens, _llm_cost_usd
    with _lock:
        _llm_prompt_tokens += int(prompt_tokens)
        _llm_completion_tokens += int(completion_tokens)
        _llm_cost_usd += float(cost_usd)


def record_composio_call() -> None:
    global _composio_calls
    with _lock:
        _composio_calls += 1


def record_cache(hit: bool) -> None:
    global _cache_hits, _cache_misses
    with _lock:
        if hit:
            _cache_hits += 1
        else:
            _cache_misses += 1


def record_extraction(n_facts: int) -> None:
    global _extraction_total
    with _lock:
        _extraction_total += int(n_facts)


# ── Percentile calculation ──────────────────────────────────────────────────────

def _percentile(p: float) -> float:
    """Compute p-th percentile (0-100) from current latency samples. Returns 0 if empty."""
    with _lock:
        samples = sorted(_lat_samples)
    if not samples:
        return 0.0
    idx = max(0, math.ceil((p / 100.0) * len(samples)) - 1)
    return samples[idx]


# ── Queue depth (Redis) ────────────────────────────────────────────────────────

def _queue_depth() -> int:
    """Read brain:queue:default LLEN from Redis. Returns 0 on any error."""
    try:
        import redis
        from brain import config
        r = redis.Redis.from_url(config.REDIS_URL, socket_connect_timeout=2, socket_timeout=2)
        depth = r.llen("brain:queue:default")
        return int(depth) if depth else 0
    except Exception:
        return 0


# ── Prometheus text format ─────────────────────────────────────────────────────

def prometheus_text() -> str:
    """
    Emit all metrics in Prometheus text exposition format.
    Includes TYPE and HELP comment lines, counters, gauges, and histograms.
    """
    lines: list[str] = []

    def _comment(name: str, help_text: str, type_: str) -> None:
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} {type_}")

    with _lock:
        rc = _request_count
        ec = _error_count
        pt = _llm_prompt_tokens
        ct = _llm_completion_tokens
        cost = _llm_cost_usd
        comp = _composio_calls
        ch = _cache_hits
        cm = _cache_misses
        ext = _extraction_total
        lc = _lat_count
        ls = _lat_sum
        lb = list(_lat_buckets)

    queue = _queue_depth()

    # request_count
    _comment("brain_request_count_total", "Total HTTP requests received", "counter")
    lines.append(f"brain_request_count_total {rc}")

    # error_count
    _comment("brain_error_count_total", "Total HTTP 5xx errors", "counter")
    lines.append(f"brain_error_count_total {ec}")

    # llm_tokens_total (prompt + completion as separate labels)
    _comment("brain_llm_tokens_total", "Total LLM tokens used", "counter")
    lines.append(f'brain_llm_tokens_total{{type="prompt"}} {pt}')
    lines.append(f'brain_llm_tokens_total{{type="completion"}} {ct}')

    # llm_cost_usd_total
    _comment("brain_llm_cost_usd_total", "Total LLM cost in USD", "counter")
    lines.append(f"brain_llm_cost_usd_total {cost:.6f}")

    # composio_calls_total
    _comment("brain_composio_calls_total", "Total Composio API calls", "counter")
    lines.append(f"brain_composio_calls_total {comp}")

    # cache_hits_total
    _comment("brain_cache_hits_total", "Total cache hits", "counter")
    lines.append(f"brain_cache_hits_total {ch}")

    # cache_misses_total
    _comment("brain_cache_misses_total", "Total cache misses", "counter")
    lines.append(f"brain_cache_misses_total {cm}")

    # extraction_throughput_total
    _comment("brain_extraction_throughput_total", "Total facts extracted from episodes", "counter")
    lines.append(f"brain_extraction_throughput_total {ext}")

    # queue_depth
    _comment("brain_queue_depth", "Current Redis job queue depth", "gauge")
    lines.append(f"brain_queue_depth {queue}")

    # latency percentiles (computed from sample deque)
    p50 = _percentile(50)
    p95 = _percentile(95)
    p99 = _percentile(99)
    _comment("brain_request_latency_p50_seconds",
             "p50 request latency in seconds (rolling window)", "gauge")
    lines.append(f"brain_request_latency_p50_seconds {p50:.6f}")

    _comment("brain_request_latency_p95_seconds",
             "p95 request latency in seconds (rolling window)", "gauge")
    lines.append(f"brain_request_latency_p95_seconds {p95:.6f}")

    _comment("brain_request_latency_p99_seconds",
             "p99 request latency in seconds (rolling window)", "gauge")
    lines.append(f"brain_request_latency_p99_seconds {p99:.6f}")

    # histogram
    _comment("brain_request_latency_seconds",
             "HTTP request latency histogram", "histogram")
    for i, upper in enumerate(_BUCKETS):
        lines.append(f'brain_request_latency_seconds_bucket{{le="{upper}"}} {lb[i]}')
    lines.append(f'brain_request_latency_seconds_bucket{{le="+Inf"}} {lb[-1]}')
    lines.append(f"brain_request_latency_seconds_count {lc}")
    lines.append(f"brain_request_latency_seconds_sum {ls:.6f}")

    lines.append("")   # trailing newline required by Prometheus
    return "\n".join(lines)
