use wasm_bindgen_test::*;

use serde_json::json;

wasm_bindgen_test_configure!(run_in_browser);

#[wasm_bindgen_test]
fn contract_glue_preserves_typed_trigger_meta() {
    let canvas = json!({
        "nodes": [
            {
                "id": "n1",
                "type": "Trigger",
                "position": { "x": 0.0, "y": 0.0 },
                "config": {
                    "trigger": {
                        "type": "Email",
                        "config": { "enabled": true, "params": {"foo": "bar"}, "filters": [] }
                    }
                }
            }
        ],
        "edges": []
    });

    let out = crate::network::contract_glue::normalize_canvas_for_api(canvas)
        .expect("normalize ok");
    let out_val = serde_json::to_value(&out).unwrap();

    let node = out_val.get("nodes").and_then(|n| n.get(0)).unwrap();
    assert_eq!(node.get("type").and_then(|v| v.as_str()), Some("trigger"));

    let cfg = node.get("config").unwrap();
    // Typed trigger meta is preserved
    let trig = cfg.get("trigger").and_then(|v| v.as_object()).expect("config.trigger present");
    assert_eq!(trig.get("type").and_then(|v| v.as_str()), Some("email"));
    let tcfg = trig.get("config").and_then(|v| v.as_object()).expect("trigger.config present");
    assert_eq!(tcfg.get("enabled").and_then(|v| v.as_bool()), Some(true));
    assert_eq!(tcfg.get("params").and_then(|v| v.get("foo")).and_then(|v| v.as_str()), Some("bar"));
    // Legacy flattened keys are not added
    assert!(cfg.get("trigger_type").is_none());
    assert!(cfg.get("params").is_none());
    assert!(cfg.get("filters").is_none());
    assert!(cfg.get("enabled").is_none());
}
