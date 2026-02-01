# Branding

## Naming Map

- **Longhouse** = public product + brand
- **Oikos** = assistant UI inside Longhouse
- **Zerg** = internal codename/repo/module (transitional)

## Usage Rules

**Do:**
- Use **Longhouse** in marketing, UI, and docs
- Use **Oikos** only for the assistant feature
- Keep CLI verbs neutral (`longhouse up`, `longhouse onboard`, `longhouse connect`)

**Don't:**
- Mix Longhouse and Zerg in user-facing copy
- Use Swarmlet or StarCraft theming in new docs
- Apply themed verbs to APIs/CLI commands

## Transition Notes

- Repo paths still live under `apps/zerg/` until the code rename lands
- Some env vars / schema names may still use `ZERG_` during transition
