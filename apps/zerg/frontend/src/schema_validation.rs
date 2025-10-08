//! Schema validation helpers – compiled into WASM.

use jsonschema::JSONSchema;
use lazy_static::lazy_static;
use serde_json::Value;

lazy_static! {
    static ref ENVELOPE_SCHEMA: JSONSchema = {
        // At compile-time embed the schema JSON string.
        let raw = include_str!("schema/envelope_schema.json");
        let parsed: Value = serde_json::from_str(raw)
            .expect("Envelope JSON schema must be valid JSON");
        JSONSchema::compile(&parsed).expect("valid envelope schema")
    };
}

/// Validate a value against the *Envelope* schema.
/// Returns `true` when valid, `false` otherwise.
pub fn validate_envelope(value: &Value) -> bool {
    ENVELOPE_SCHEMA.validate(value).is_ok()
}
