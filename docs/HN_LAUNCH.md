# Hacker News Launch Draft

## Title Options

1. **Show HN: Longhouse – Search across all your AI coding sessions**
   - Clear, direct, shows the value

2. **Show HN: Never lose a Claude Code conversation again**
   - Problem-focused, relatable pain point
   - Might sound too specific to Claude

3. **Show HN: Timeline view for Claude, Cursor, and Codex sessions**
   - Feature-focused, shows multi-tool support

4. **Show HN: Longhouse – Unified timeline for all your AI coding tools**
   - Comprehensive, maybe too wordy

**Recommendation:** #1 or #2

---

## Launch Comment (First Comment)

```
Hey HN! I built Longhouse to solve a problem I kept hitting: losing track of AI coding conversations.

THE PROBLEM:
I use Claude Code, Codex, and Cursor daily. Each tool stores sessions in different JSONL files scattered across ~/.claude/projects/*, ~/.codex/sessions/*, etc. When I need to find "that conversation from last week where the AI fixed my auth bug," I end up grepping through thousands of lines of JSON. Or worse, I just give up and ask the AI to solve it again from scratch.

THE SOLUTION:
Longhouse watches these session files and unifies them into a single, searchable timeline. Now I can:
- Search across all tools: "Show me authentication-related sessions"
- Find by project/date: "What did I work on in longhouse last Tuesday?"
- See tool usage: "How many times did I run tests during that refactor?"
- Resume work: "What was I doing before I got interrupted?"

TECH:
- Python 3.12+ backend (FastAPI, SQLAlchemy)
- SQLite for local-first storage
- React frontend
- CLI for session syncing (watches JSONL files, ingests into DB)
- One-liner install: `pip install longhouse && longhouse onboard`

CURRENT STATE:
- ✅ Works with Claude Code (real-time syncing)
- 🚧 Codex, Cursor, Gemini support in progress
- ✅ Local-first (your data never leaves your machine)
- ✅ Search by content, project, date, tool
- 🚧 Cross-session resume (continue a conversation in a different tool)

It's alpha-quality but usable. I've been dogfooding it for 3 weeks and it's already saved me hours of searching through old sessions.

The repo is public, MIT licensed, and the install should work on macOS and Linux (WSL for Windows).

Try it: `pip install longhouse && longhouse onboard`

Feedback welcome! Especially interested in:
- What other AI tools should I support first?
- What features would make this indispensable for you?
- Any bugs or rough edges you hit

GitHub: https://github.com/cipher982/longhouse
```

---

## Timing Recommendations

**Best days:** Tuesday, Wednesday, Thursday
**Best times:** 8-10am PT (when HN traffic peaks)
**Avoid:** Monday (too busy), Friday (low engagement), weekends

**Engagement strategy:**
- Respond to every comment within first 2 hours
- Be technical, honest about limitations
- Show roadmap openness
- Thank people for feedback
- Address concerns directly

---

## Anticipated Questions & Answers

**Q: Why not just use Claude Code's built-in history?**
A: Claude Code history is:
- Limited to Claude only
- No cross-session search
- No project filtering
- Doesn't persist if you clear cache

**Q: How does this compare to X?**
A: Most similar to session managers for terminal (tmux/screen) but for AI tools. There's no direct competitor I know of that unifies multiple AI coding tools.

**Q: Privacy concerns?**
A: Everything is local-first. SQLite database lives in ~/.longhouse/. No cloud, no telemetry. Your sessions never leave your machine unless you explicitly configure remote sync (optional feature).

**Q: Performance with thousands of sessions?**
A: SQLite handles this well. I have ~500 sessions locally and search is instant. Database is indexed properly.

**Q: What about other tools like Cursor?**
A: Working on it! The architecture is tool-agnostic. Need to add parsers for each tool's session format. PRs welcome.

**Q: Can I export my data?**
A: Yes, SQLite database is standard. Can export to JSON via API or just copy the .db file.

---

## Pre-Launch Checklist

Before posting:
- [ ] README has screenshot (done!)
- [ ] Demo data seeds on first run (done!)
- [ ] PyPI package works (verified)
- [ ] Installer works on fresh machines (CI passing)
- [ ] Production is healthy (check before post)
- [ ] Clear next steps in README (done!)

---

## Fallback Plans

If HN doesn't gain traction:
- Post to /r/programming
- Tweet with demo video
- Product Hunt launch
- Reach out to AI tool communities directly
