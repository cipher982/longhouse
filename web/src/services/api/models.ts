import { request } from "./base";
import type { ModelConfig } from "./types";

export async function fetchModels(): Promise<ModelConfig[]> {
  // Avoid FastAPI's trailing-slash redirect (which can miscompute scheme behind proxies)
  return request<ModelConfig[]>(`/models/`);
}
