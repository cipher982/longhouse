# Multi-Provider Model Support - Handoff Doc

## Current State (as of 2026-01-15)

Groq is supported alongside OpenAI. OpenRouter references were removed from the runtime code path. This doc notes what exists, what was fixed, and what remains.

---

## 1. Model Configuration Flow

### Where models are defined

```
config/models.json                    # Source of truth for all models
    ↓
apps/zerg/backend/zerg/models_config.py   # Loads JSON, creates ModelConfig objects
    ↓
/api/models endpoint                  # Serves to frontend
    ↓
Frontend model selector dropdown      # User picks model
```

### Current `models.json` structure

```json
{
  "text": {
    "models": {
      "gpt-5.2": {
        "displayName": "GPT-5.2",
        "provider": "openai",
        "tier": "TIER_1",
        "description": "...",
        "capabilities": {
          "reasoning": true,
          "reasoningNone": true
        }
      },
      "qwen/qwen3-32b": {
        "displayName": "Qwen 3 32B (Groq)",
        "provider": "groq",
        "baseUrl": "https://api.groq.com/openai/v1",
        "capabilities": {
          "reasoning": true,
          "reasoningNone": true
        }
      }
    }
  }
}
```

### Provider routing in `_make_llm()`

Location: `apps/zerg/backend/zerg/services/supervisor_react_engine.py:168-199`

```python
provider = model_config.provider.value if model_config else "openai"

if provider == "groq":
    api_key = settings.groq_api_key
    base_url = model_config.base_url
else:
    api_key = settings.openai_api_key
    base_url = None
```

---

## 2. Known Issues / Gaps (Updated)

### Issue 1: Reasoning selector shows for all models

**Status:** Resolved.
- Frontend filters by `model.capabilities.reasoning` (`ModelSelector.tsx`)
- Backend only passes `reasoning_effort` when supported (`supervisor_react_engine.py`)

### Issue 2: Model capability metadata depth

**Status:** Partial.
- `capabilities` exists in `models.json` (currently `reasoning` / `reasoningNone`)
- If we need tool-calling/vision/context-window metadata, extend the schema and UI

### Issue 3: Model ID validation

**Status:** Resolved.
- `_make_llm()` validates model IDs and raises with available models if unknown

### Issue 4: API key validation

**Status:** Partial.
- Groq models check `GROQ_API_KEY` at runtime and raise a clear error
- No startup validation or auto-filtering of available models yet

### Issue 5: E2E coverage for model UX

**Status:** Partial.
- `apps/zerg/e2e/tests/model-capabilities.spec.ts` covers model list + reasoning selector behavior
- No provider-specific chat smoke test yet (optional add if we want runtime coverage)

---

## 3. Files Involved

| File | Purpose |
|------|---------|
| `config/models.json` | Model definitions (source of truth) |
| `apps/zerg/backend/zerg/models_config.py` | Loads config, `ModelConfig` class, `ModelProvider` enum |
| `apps/zerg/backend/zerg/config/__init__.py` | `Settings` class with API keys |
| `apps/zerg/backend/zerg/services/supervisor_react_engine.py` | `_make_llm()` - creates LLM instances |
| `apps/zerg/backend/zerg/routers/models.py` | `/api/models` endpoint |
| `apps/zerg/frontend-web/src/jarvis/app/components/ChatHeader.tsx` | Model selector UI (verify actual location) |
| `docker/docker-compose.dev.yml` | Env vars passed to containers |
| `.env` | API keys |

---

## 4. Provider-Specific Details

### OpenAI
- Base URL: Default (https://api.openai.com/v1)
- API key env: `OPENAI_API_KEY`
- Special params: `reasoning_effort` (for o1, o3, gpt-5.2)
- Headers: None special

### Groq
- Base URL: `https://api.groq.com/openai/v1`
- API key env: `GROQ_API_KEY`
- Special params: None
- Headers: None special
- Notes: Blazing fast (LPU), limited model selection

### OpenRouter (not supported)
- Removed from the runtime code path as of 2026-01-15
- If re-adding: base URL `https://openrouter.ai/api/v1`, env `OPENROUTER_API_KEY`,
  and required attribution headers (`HTTP-Referer`, `X-Title`)

---

## 5. Proper Implementation Checklist

### Phase 1: Fix UI (reasoning selector)
- [x] Add `supportsReasoning` to model config schema
- [x] Update `models.json` with capability flags
- [x] Update `ModelConfig` class to include capabilities
- [x] Update frontend to conditionally show reasoning selector
- [x] Test: reasoning selector hidden for Groq models

### Phase 2: Add model capabilities (partial: reasoning only)
- [ ] Design capabilities schema (toolCalling, streaming, reasoning, vision, contextWindow)
- [ ] Add to all models in `models.json`
- [ ] Expose via `/api/models` endpoint
- [ ] Frontend can use for feature gating

### Phase 3: Validation
- [x] Validate model ID exists before LLM call
- [ ] Validate required API key exists for provider (startup or filtering)
- [x] Return helpful error messages

### Phase 4: E2E Tests
- [ ] Test: Select OpenAI model, send message, get response
- [ ] Test: Select Groq model, send message, get response
- [x] Test: Reasoning selector hidden for non-reasoning models
- [ ] Test: Error handling when API key missing

### Phase 5: Documentation
- [ ] Update AGENTS.md with model configuration instructions
- [ ] Document how to add a new provider
- [ ] Document how to add a new model

---

## 6. Adding a New Model (Current Process)

1. **Add to `config/models.json`:**
```json
"model-id": {
  "displayName": "Human Name",
  "provider": "openai|groq",
  "description": "...",
  "baseUrl": "https://..." // if not OpenAI
}
```

2. **If new provider, add to `ModelProvider` enum:**
```python
# models_config.py
class ModelProvider(str, Enum):
    OPENAI = "openai"
    GROQ = "groq"
    NEW_PROVIDER = "new_provider"
```

3. **If new provider, add API key to Settings:**
```python
# config/__init__.py
@dataclass
class Settings:
    new_provider_api_key: Any

# In _load_settings():
new_provider_api_key=os.getenv("NEW_PROVIDER_API_KEY"),
```

4. **If new provider, update `_make_llm()`:**
```python
# supervisor_react_engine.py
elif provider == "new_provider":
    api_key = settings.new_provider_api_key
    base_url = model_config.base_url
```

5. **Add env var to docker-compose:**
```yaml
# docker-compose.dev.yml
NEW_PROVIDER_API_KEY: ${NEW_PROVIDER_API_KEY:-}
```

6. **Add to `.env`:**
```
NEW_PROVIDER_API_KEY=your-key-here
```

7. **Restart backend, test in UI**

---

## 7. Testing Recommendations

### Manual Testing (current)
- Select each provider's model
- Send simple message ("What is 2+2?")
- Verify response
- Check no errors in backend logs

### Automated E2E Testing (needed)
```typescript
// apps/zerg/e2e/tests/multi-provider.spec.ts

test.describe('Multi-provider models', () => {
  test('OpenAI model works', async ({ page }) => {
    await page.goto('/chat');
    await page.getByLabel('Select model').selectOption('gpt-5.2');
    await page.getByTestId('chat-input').fill('Say "hello"');
    await page.getByTestId('send-message-btn').click();
    await expect(page.locator('.assistant-message')).toContainText('hello', { timeout: 30000 });
  });

  test('Groq model works', async ({ page }) => {
    await page.goto('/chat');
    await page.getByLabel('Select model').selectOption('qwen/qwen3-32b');
    await page.getByTestId('chat-input').fill('Say "hello"');
    await page.getByTestId('send-message-btn').click();
    await expect(page.locator('.assistant-message')).toContainText('hello', { timeout: 30000 });
  });

  test('reasoning selector hidden for non-OpenAI models', async ({ page }) => {
    await page.goto('/chat');
    await page.getByLabel('Select model').selectOption('qwen/qwen3-32b');
    await expect(page.getByLabel('Reasoning effort')).toBeHidden();
  });
});
```

---

## 8. Open Questions

1. **Model allowlisting:** `ALLOWED_MODELS_NON_ADMIN` env var exists - does it work with Groq models?

2. **Cost tracking:** Different providers have different costs. Do we need per-provider cost tracking?

3. **Rate limiting:** Groq has aggressive rate limits on free tier. Should we handle 429s gracefully?

---

## Summary

**What works:**
- Provider routing (OpenAI, Groq)
- Model selection in UI
- Reasoning selector gated by capabilities
- Model ID validation + clearer API key errors

**What's broken/missing:**
- Limited capabilities metadata (reasoning only)
- No startup key validation / model filtering
- No provider-specific chat E2E smoke tests
