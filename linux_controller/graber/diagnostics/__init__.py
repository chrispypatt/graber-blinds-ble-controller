"""Read-only host diagnostics and operation reports."""

from .adapters import (
    AdapterIdentity,
    CheckResult,
    CommandRunner,
    adapter_as_dict,
    adapter_preflight,
    discover_adapters,
    resolve_configured_adapters,
    run_host_checks,
    select_adapter,
    redact_address,
)
from .metrics import (
    JsonlMetricsWriter,
    filter_records,
    load_jsonl,
    load_jsonl_files,
    parse_time_boundary,
)
from .report import (
    LATENCY_FIELDS,
    percentile,
    summarize_operations,
)

__all__ = [
    "AdapterIdentity",
    "CheckResult",
    "CommandRunner",
    "JsonlMetricsWriter",
    "LATENCY_FIELDS",
    "adapter_as_dict",
    "adapter_preflight",
    "discover_adapters",
    "filter_records",
    "load_jsonl",
    "load_jsonl_files",
    "parse_time_boundary",
    "percentile",
    "redact_address",
    "resolve_configured_adapters",
    "run_host_checks",
    "select_adapter",
    "summarize_operations",
]
