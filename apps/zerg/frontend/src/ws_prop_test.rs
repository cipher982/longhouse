//! Internal unit-style property test (compiled to WASM) that ensures the
//! envelope validator accepts any schema-conform JSON object.

#![cfg(test)]

use proptest::prelude::*;
use serde_json::Value;
use wasm_bindgen_test::*;

use crate::generated::ws_messages::Envelope;
use crate::generated::ws_messages::validate_envelope;

// Make wasm-bindgen execute the tests in a headless browser when available.
wasm_bindgen_test_configure!(run_in_browser);

/// Strategy producing small arbitrary JSON objects used as the `data` field.
fn json_object_strategy() -> impl Strategy<Value = Value> {
    let leaf = "[a-z0-9]{0,12}";
    prop::collection::hash_map(leaf, leaf, 0..8).prop_map(|map| {
        Value::Object(
            map.into_iter()
                .map(|(k, v)| (k, Value::String(v)))
                .collect(),
        )
    })
}

/// Full envelope generator.
fn envelope_strategy() -> impl Strategy<Value = Envelope> {
    let type_strat = prop_oneof![
        Just("PING".to_string()),
        Just("PONG".to_string()),
        Just("THREAD_MESSAGE".to_string()),
        Just("STREAM_CHUNK".to_string()),
        Just("AGENT_EVENT".to_string()),
    ];

    let topic_strat = prop_oneof![
        (1u32..10_000u32).prop_map(|id| format!("thread:{}", id)),
        (1u32..10_000u32).prop_map(|id| format!("agent:{}", id)),
        Just("system".to_string()),
    ];

    (type_strat, topic_strat, json_object_strategy(), any::<u64>(), proptest::option::of("[a-z0-9]{0,10}"))
        .prop_map(|(ty, topic, data, ts, req_id)| Envelope {
            v: 1,
            r#type: ty,
            topic,
            req_id,
            ts,
            data,
        })
}

#[wasm_bindgen_test]
fn envelope_validation_fuzz() {
    let mut runner = proptest::test_runner::TestRunner::default();
    let strategy = envelope_strategy();

    runner
        .run(&strategy, |env| {
            let val = serde_json::to_value(&env).expect("serialize envelope");
            assert!(validate_envelope(&val));
            Ok(())
        })
        .expect("property test failed");
}
