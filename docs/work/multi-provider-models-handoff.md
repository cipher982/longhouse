# Multi-Provider Model Support - Handoff Doc

## Current State (as of 2026-01-14)

We added OpenRouter and Groq as providers, but the implementation has gaps. This doc covers what exists, what's broken, and what needs proper implementation.

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
        "baseUrl": null  // optional - only for non-OpenAI
      },
      "qwen/qwen3-32b": {
        "displayName": "Qwen 3 32B (Groq)",
        "provider": "groq",
        "baseUrl": "https://api.groq.com/openai/v1"
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
elif provider == "openrouter":
    api_key = settings.openrouter_api_key
    base_url = model_config.base_url
else:
    api_key = settings.openai_api_key
    base_url = None
```

---

## 2. Known Issues / Gaps

### Issue 1: Reasoning selector shows for all models

**Problem:** The UI shows "Reasoning effort: None/Low/Medium/High" for ALL models, but only OpenAI reasoning models support this parameter.

**Where it breaks:**
- Frontend: `src/jarvis/app/components/ModelSelector.tsx` (or similar) - doesn't filter by provider
- Backend: We pass `reasoning_effort` to `ChatOpenAI` even for non-OpenAI - it's ignored but confusing

**Fix needed:**
1. Add `supportsReasoning: boolean` to model config
2. Frontend: Hide reasoning selector when model doesn't support it
3. Backend: Only pass `reasoning_effort` when provider supports it (already partially done)

### Issue 2: No model capability metadata

**Problem:** Different models support different features:
- Tool calling (most do, but not all)
- Streaming (most do)
- Reasoning effort (OpenAI only)
- Vision/images (some models)
- Context window size (varies wildly)

**Current state:** We have none of this metadata. We just assume all models work the same.

**Fix needed:** Add to `models.json`:
```json
{
  "gpt-5.2": {
    "capabilities": {
      "toolCalling": true,
      "streaming": true,
      "reasoning": true,
      "vision": true,
      "contextWindow": 128000
    }
  },
  "qwen/qwen3-32b": {
    "capabilities": {
      "toolCalling": true,
      "streaming": true,
      "reasoning": false,
      "vision": false,
      "contextWindow": 32000
    }
  }
}
```

### Issue 3: No validation of model IDs

**Problem:** If someone types a wrong model ID in the UI or API, we just pass it through and let OpenAI/Groq error out.

**Fix needed:** Validate model ID exists in our config before making LLM calls.

### Issue 4: API keys not validated at startup

**Problem:** If `GROQ_API_KEY` is missing but a user selects a Groq model, they get a cryptic runtime error.

**Fix needed:** Either:
- Validate all provider API keys at startup (strict)
- Or filter available models based on which API keys are configured (flexible)

### Issue 5: No E2E tests for multi-provider

**Problem:** We tested manually via browser, but no automated tests exist.

**Fix needed:** Add E2E tests:
```typescript
test('can chat with Groq model', async () => {
  // Select Groq model
  // Send message
  // Verify response received
  // Verify no errors
});
```

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

### OpenRouter
- Base URL: `https://openrouter.ai/api/v1`
- API key env: `OPENROUTER_API_KEY`
- Special params: None (provider routing is in request body, not supported yet)
- Headers: `HTTP-Referer`, `X-Title` (for attribution/rankings)
- Notes: Routes to multiple backends, can't guarantee which one

---

## 5. Proper Implementation Checklist

### Phase 1: Fix UI (reasoning selector)
- [ ] Add `supportsReasoning` to model config schema
- [ ] Update `models.json` with capability flags
- [ ] Update `ModelConfig` class to include capabilities
- [ ] Update frontend to conditionally show reasoning selector
- [ ] Test: reasoning selector hidden for Groq models

### Phase 2: Add model capabilities
- [ ] Design capabilities schema (toolCalling, streaming, reasoning, vision, contextWindow)
- [ ] Add to all models in `models.json`
- [ ] Expose via `/api/models` endpoint
- [ ] Frontend can use for feature gating

### Phase 3: Validation
- [ ] Validate model ID exists before LLM call
- [ ] Validate required API key exists for provider
- [ ] Return helpful error messages

### Phase 4: E2E Tests
- [ ] Test: Select OpenAI model, send message, get response
- [ ] Test: Select Groq model, send message, get response
- [ ] Test: Reasoning selector hidden for non-reasoning models
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
  "provider": "openai|groq|openrouter",
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

1. **Should we remove OpenRouter?** It adds complexity and doesn't guarantee Groq. If we have direct Groq, maybe remove OpenRouter models?

2. **Model allowlisting:** `ALLOWED_MODELS_NON_ADMIN` env var exists - does it work with new providers?

3. **Cost tracking:** Different providers have different costs. Do we need per-provider cost tracking?

4. **Rate limiting:** Groq has aggressive rate limits on free tier. Should we handle 429s gracefully?

---

## Summary

**What works:**
- Basic provider routing (OpenAI, Groq, OpenRouter)
- Model selection in UI
- API calls go to correct provider

**What's broken/missing:**
- Reasoning selector shows for all models (should be OpenAI only)
- No model capabilities metadata
- No validation
- No automated tests
- No documentation

**Priority fix:** Hide reasoning selector for non-reasoning models - it's confusing users.
