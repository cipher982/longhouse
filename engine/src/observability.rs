//! Minimal OpenTelemetry bootstrap for the Rust engine.
//!
//! Rules:
//! - opt-in only via standard OTLP endpoint env vars
//! - traces only for now
//! - no transcript content, file paths, or payload bodies in span attributes

use crate::build_identity::BuildIdentity;
use opentelemetry::trace::TracerProvider as _;
use opentelemetry::{global, KeyValue};
use opentelemetry_otlp::SpanExporter;
use opentelemetry_sdk::trace::{BatchConfigBuilder, BatchSpanProcessor, SdkTracerProvider};
use opentelemetry_sdk::Resource;

pub struct OtelSetup {
    pub tracer: opentelemetry_sdk::trace::Tracer,
    pub guard: OtelGuard,
}

pub struct OtelGuard {
    tracer_provider: SdkTracerProvider,
}

impl Drop for OtelGuard {
    fn drop(&mut self) {
        if let Err(err) = self.tracer_provider.shutdown() {
            eprintln!("failed to shut down engine OpenTelemetry tracer provider: {err:?}");
        }
    }
}

fn truthy(value: Option<String>) -> bool {
    value
        .as_deref()
        .map(str::trim)
        .map(str::to_ascii_lowercase)
        .is_some_and(|value| matches!(value.as_str(), "1" | "true" | "yes" | "on"))
}

fn otlp_endpoint_configured() -> bool {
    std::env::var("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
        .ok()
        .filter(|value| !value.trim().is_empty())
        .is_some()
        || std::env::var("OTEL_EXPORTER_OTLP_ENDPOINT")
            .ok()
            .filter(|value| !value.trim().is_empty())
            .is_some()
}

fn default_instance_id() -> String {
    format!("pid-{}", std::process::id())
}

fn trim_otel_endpoint_vars() {
    for key in [
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
    ] {
        if let Ok(value) = std::env::var(key) {
            let trimmed = value.trim().to_string();
            if trimmed != value && !trimmed.is_empty() {
                std::env::set_var(key, trimmed);
            }
        }
    }
}

fn build_resource(command_name: &'static str) -> Resource {
    let build = BuildIdentity::current();
    Resource::builder_empty()
        .with_attributes([
            KeyValue::new("service.name", "longhouse-engine"),
            KeyValue::new("service.version", build.qualified()),
            KeyValue::new("service.instance.id", default_instance_id()),
            KeyValue::new("longhouse.command", command_name),
            KeyValue::new("longhouse.build.channel", build.channel),
            KeyValue::new("longhouse.build.commit", build.commit_short),
            KeyValue::new("longhouse.build.dirty", build.dirty),
            KeyValue::new("longhouse.build.qualified_version", build.qualified()),
        ])
        .build()
}

pub fn build_otel_setup(command_name: &'static str) -> anyhow::Result<Option<OtelSetup>> {
    if truthy(std::env::var("OTEL_SDK_DISABLED").ok()) {
        return Ok(None);
    }
    if !otlp_endpoint_configured() {
        return Ok(None);
    }
    trim_otel_endpoint_vars();

    let exporter = SpanExporter::builder().with_http().build()?;
    let batch_processor = BatchSpanProcessor::builder(exporter)
        .with_batch_config(
            BatchConfigBuilder::default()
                .with_max_queue_size(8_192)
                .with_max_export_batch_size(1_024)
                .with_scheduled_delay(std::time::Duration::from_secs(5))
                .build(),
        )
        .build();
    let tracer_provider = SdkTracerProvider::builder()
        .with_resource(build_resource(command_name))
        .with_span_processor(batch_processor)
        .build();
    let tracer = tracer_provider.tracer("longhouse-engine");
    global::set_tracer_provider(tracer_provider.clone());

    Ok(Some(OtelSetup {
        tracer,
        guard: OtelGuard { tracer_provider },
    }))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn otlp_endpoint_configured_checks_traces_endpoint() {
        let _guard = temp_env::with_var(
            "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
            Some("http://127.0.0.1:4318/v1/traces"),
            || {
                assert!(otlp_endpoint_configured());
            },
        );
    }

    #[test]
    fn build_otel_setup_is_disabled_without_endpoint() {
        temp_env::with_vars(
            [
                ("OTEL_EXPORTER_OTLP_ENDPOINT", None::<String>),
                ("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", None::<String>),
                ("OTEL_SDK_DISABLED", None::<String>),
            ],
            || {
                assert!(build_otel_setup("connect").unwrap().is_none());
            },
        );
    }
}
