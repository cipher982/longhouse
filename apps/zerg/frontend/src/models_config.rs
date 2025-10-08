use serde::{Deserialize, Serialize};

// Model configuration as received from the backend API
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ModelConfig {
    pub id: String,
    pub display_name: String,
    pub provider: String,
    pub is_default: bool,
}

// Parse models from API response
#[allow(dead_code)]
pub fn parse_models_from_api(models: Vec<ModelConfig>) -> Vec<(String, String)> {
    models
        .into_iter()
        .map(|model| (model.id, model.display_name))
        .collect()
}
