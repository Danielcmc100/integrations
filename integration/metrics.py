from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

# Default registry — replaced in tests via _make_registry()
REGISTRY = CollectorRegistry()

webhooks_received_total: Counter = Counter(
    "webhooks_received_total",
    "Total webhooks received by source",
    ["source"],
    registry=REGISTRY,
)

sync_actions_total: Counter = Counter(
    "sync_actions_total",
    "Total sync actions completed",
    ["type", "outcome"],
    registry=REGISTRY,
)

sync_duration_seconds: Histogram = Histogram(
    "sync_duration_seconds",
    "Duration of sync actions in seconds",
    ["type"],
    registry=REGISTRY,
)

arq_queue_depth: Gauge = Gauge(
    "arq_queue_depth",
    "Current number of jobs in the ARQ queue",
    registry=REGISTRY,
)
