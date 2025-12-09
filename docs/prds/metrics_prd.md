⏺ Product Requirements Document: Modern Observability Infrastructure
Implementation

Executive Summary

This PRD outlines the implementation of a modern, event-first observability
stack for the Zerg platform, replacing traditional metrics-first monitoring
approaches with a raw-data collection system optimized for WebSocket-heavy,
real-time applications.

Background & Context

Current System Architecture

- Backend: FastAPI with extensive WebSocket usage for real-time agent
  communication
- Frontend: Rust/WASM with complex WebSocket message handling
- Key Challenge: Previous API spam issue (GET /api/agents) caused by WebSocket
  fallback mechanisms
- Data Patterns: High-frequency WebSocket messages, agent execution events,
  workflow state changes

Problem Statement

Traditional monitoring approaches (Prometheus histograms, pre-aggregated
metrics) are insufficient for:

1. High-cardinality data from WebSocket events with user/agent/session
   dimensions
2. Retroactive analysis requirements for debugging complex distributed flows
3. Cost-effective storage of high-volume, real-time event streams
4. Flexible querying without pre-defining metric aggregations

Solution Architecture

Core Philosophy: Event-First Observability

- Collect everything as raw events - no data loss from bucketing/aggregation
- Schema-on-read - define queries when needed, not during collection
- Trace-driven - leverage distributed tracing for request flow correlation
- Cost-optimized - columnar storage with automatic tiering

Technology Stack Selection

1. Data Collection Layer

OpenTelemetry (OTel) as universal instrumentation standard

- Rationale: Industry standard, vendor-neutral, auto-instrumentation available
- WebSocket Integration: Custom trace context propagation over WebSocket
  headers
- WASM Considerations: OTel WASM SDK in alpha; fallback to manual event
  emission

2. Data Pipeline Layer

Vector for observability data processing

- Rationale: 10x performance advantage, Rust-based reliability, zero vendor
  lock-in
- Capabilities: Real-time transformation, routing, enrichment without code
  changes
- Integration: Receives OTel traces, application logs, system metrics

3. Storage Layer

ClickHouse for raw event storage

- Rationale: Columnar storage, 40x compression, petabyte-scale performance
- Query Performance: Sub-second SQL queries on raw events
- Cost Efficiency: 140x lower storage costs vs. Elasticsearch
- Tiering Strategy: Hot (memory) → Warm (disk) → Cold (S3 Parquet)

4. Analysis Layer

Grafana + ClickHouse Datasource for visualization

- Rationale: Familiar interface, SQL-based queries, flexible dashboard
  creation
- Alternative: HyperDX for SaaS experience with ClickHouse backend

Implementation Phases

Phase 1: Foundation (Week 1-2)

Objective: Basic event collection and storage

Backend Instrumentation

# Auto-instrumentation setup

pip install opentelemetry-distro[auto_patcher] opentelemetry-exporter-otlp
opentelemetry-bootstrap -a integrate

# Environment configuration

OTEL_SERVICE_NAME=zerg-backend
OTEL_EXPORTER_OTLP_ENDPOINT=http://vector:4318

WebSocket Event Correlation

# Custom WebSocket trace propagation

async def send_websocket_message(websocket, data):
current_span = trace.get_current_span()
trace_context = {}
TraceContextTextMapPropagator().inject(trace_context)

    message = {
        "data": data,
        "trace_context": trace_context,
        "timestamp": time.time()
    }
    await websocket.send_json(message)

Infrastructure Deployment

# Docker Compose services

services:
vector:
image: timberio/vector:latest
ports: ["4318:4318"] # OTel receiver

clickhouse:
image: clickhouse/clickhouse-server:latest
ports: ["8123:8123"]

grafana:
image: grafana/grafana:latest
environment: - GF_INSTALL_PLUGINS=grafana-clickhouse-datasource

Phase 2: Frontend Integration (Week 3)

Objective: Rust/WASM event collection

WASM Event Emission

// Fallback for alpha OTel WASM SDK #[wasm_bindgen]
pub fn emit_websocket_event(event_type: &str, duration_ms: f64, attrs:
JsValue) {
let event = json!({
"timestamp": js_sys::Date::now(),
"event_type": event_type,
"duration_ms": duration_ms,
"attributes": serde_wasm_bindgen::from_value(attrs).unwrap()
});

    send_to_vector_http(event);

}

Vector Configuration Enhancement

[sources.frontend_events]
type = "http"
address = "0.0.0.0:8080"

[transforms.enrich_frontend]
type = "remap"
inputs = ["frontend_events"]
source = '''
.service = "zerg-frontend"
.environment = get_env_var("ENVIRONMENT") ?? "development"
if .event_type == "websocket_message_processed" {
.is_critical_path = true
}
'''

Phase 3: Advanced Analytics (Week 4)

Objective: Business intelligence and alerting

Key Performance Indicators

-- WebSocket message processing latency
SELECT
toStartOfMinute(timestamp) as minute,
quantile(0.50)(duration_ms) as p50,
quantile(0.95)(duration_ms) as p95,
quantile(0.99)(duration_ms) as p99
FROM websocket_events
WHERE event_type = 'websocket_message_processed'
AND timestamp >= now() - INTERVAL 1 HOUR
GROUP BY minute
ORDER BY minute;

-- Agent execution success rates by type
SELECT
attributes['agent.type'] as agent_type,
countIf(attributes['success'] = 'true') / count() \* 100 as success_rate,
count() as total_executions
FROM agent_events
WHERE event_type = 'agent_execution_completed'
AND timestamp >= now() - INTERVAL 24 HOUR
GROUP BY agent_type;

Alerting Configuration

# Vector-based alerting

[sinks.alerts]
type = "webhook"
inputs = ["transformed_events"]
uri = "https://hooks.slack.com/..."
condition = '.attributes.error_rate > 0.05'

Technical Specifications

Data Schema Design

-- Unified events table
CREATE TABLE zerg_events (
timestamp DateTime64(3),
trace_id String,
span_id String,
parent_span_id String,
service_name String,
event_type String,
attributes Map(String, String),
resource_attributes Map(String, String),
duration_ms Nullable(Float64),
status_code String
) ENGINE = MergeTree()
PARTITION BY toYYYYMM(timestamp)
ORDER BY (service_name, event_type, timestamp)
SETTINGS index_granularity = 8192;

Vector Pipeline Configuration

[sources.otlp_traces]
type = "otlp"
mode = "server"
address = "0.0.0.0:4318"

[sources.application_logs]
type = "file"
include = ["/var/log/zerg/*.log"]
read_from = "beginning"

[transforms.parse_logs]
type = "remap"
inputs = ["application_logs"]
source = '''
parsed = parse_json(.message) ?? {}
.timestamp = parsed.timestamp ?? now()
.event_type = parsed.event_type ?? "log"
.attributes = parsed.attributes ?? {}
'''

[sinks.clickhouse_events]
type = "clickhouse"
inputs = ["otlp_traces", "parse_logs"]
endpoint = "http://clickhouse:8123"
table = "zerg_events"
skip_unknown_fields = true

Performance Requirements

- Event Ingestion: 10,000+ events/second sustained
- Query Latency: <1 second for dashboard queries (24h data)
- Storage Retention: 90 days hot, 1 year warm, 5 years cold
- Availability: 99.9% uptime for data collection

Security Considerations

- Data Privacy: Configurable PII scrubbing in Vector transforms
- Access Control: Role-based access to ClickHouse queries
- Encryption: TLS for all data transport, at-rest encryption for storage
- Compliance: GDPR-compliant data deletion capabilities

Success Metrics

Technical KPIs

- Data Collection Coverage: >95% of critical application events captured
- Query Performance: <1s p95 latency for standard dashboard queries
- Storage Efficiency: >30x compression ratio vs. raw JSON logs
- System Reliability: <0.1% data loss rate

Business KPIs

- Incident Detection: Mean time to detection <5 minutes
- Root Cause Analysis: 80% reduction in debugging time
- Cost Efficiency: <50% of previous monitoring infrastructure costs
- Developer Productivity: Self-service analytics capabilities

Risk Assessment & Mitigation

Technical Risks

1. ClickHouse Learning Curve

- Mitigation: Start with simple queries, provide SQL training

2. WebSocket Trace Propagation Complexity

- Mitigation: Implement custom propagation with fallback mechanisms

3. WASM OTel SDK Maturity

- Mitigation: Use HTTP-based fallback for frontend events

Operational Risks

1. Data Volume Growth

- Mitigation: Implement automatic retention policies and sampling

2. Query Performance Degradation

- Mitigation: Proper partitioning, indexing, and query optimization

Future Considerations

Emerging Technologies

- Continuous Profiling: Parca integration for CPU/memory profiling
- AI-Powered Analytics: Anomaly detection and automated root cause analysis
- Real-Time Streaming: Apache Arrow + DataFusion for sub-second aggregations

Scalability Path

- Multi-Region Deployment: ClickHouse distributed clusters
- Edge Collection: Vector edge nodes for global deployments
- Data Lake Integration: Apache Iceberg for long-term analytical storage

This observability infrastructure will provide the foundation for data-driven
decision making while maintaining cost efficiency and operational simplicity.
