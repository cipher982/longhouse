import { usePageMeta } from "../../hooks/usePageMeta";
import { CodeBlock } from "./CodeBlock";

export default function SearchPage() {
  usePageMeta({
    title: "Search & Recall - Longhouse Docs",
    description: "Full-text search across all your AI coding sessions.",
  });

  return (
    <>
      <h1>Search & Recall</h1>
      <p className="docs-subtitle">
        Find the session where you already solved the problem instead of
        starting from scratch.
      </p>

      <h2>What gets indexed</h2>
      <p>
        Longhouse builds a full-text search index over every session it
        imports. The index covers:
      </p>
      <ul>
        <li>
          <strong>Conversation messages</strong> — both user prompts and
          assistant responses
        </li>
        <li>
          <strong>Tool calls and outputs</strong> — file edits, bash commands,
          search results, and every other tool the model used
        </li>
        <li>
          <strong>Session metadata</strong> — project name, git branch,
          working directory, timestamps
        </li>
      </ul>

      <h2>Search from the browser</h2>
      <p>
        The timeline page has a search bar at the top. Type any keyword, file
        name, function name, error message, or natural language description.
        Results are ranked by relevance and recency.
      </p>
      <p>
        Use the sidebar filters to narrow by date range, provider (Claude,
        Codex, Antigravity, OpenCode; Gemini for legacy archives), or project.
      </p>

      <h2>Search from the API</h2>
      <CodeBlock title="terminal">
        {`curl "http://localhost:8080/api/agents/sessions?query=retry+logic"`}
      </CodeBlock>
      <p>
        The same search index powers the browser, CLI, and API. All three
        return the same results.
      </p>

      <h2>Recall</h2>
      <p>
        Recall is semantic search — it finds sessions by meaning, not just
        keywords. When you search for "how did I handle rate limiting," recall
        finds sessions where you solved rate limiting even if those exact words
        were never used.
      </p>
      <CodeBlock title="terminal">
        {`longhouse recall "how did I handle auth token refresh"
longhouse recall "the session where I fixed the CI pipeline"`}
      </CodeBlock>
      <div className="docs-callout">
        <p>
          <strong>Recall vs. search:</strong> Search matches exact keywords.
          Recall matches meaning. Use search when you know the exact term;
          use recall when you remember what happened but not the words.
        </p>
        <p>
          <strong>See also:</strong> Use{" "}
          <code>longhouse wall</code> to see active and recent sessions across
          all projects — a quick way to find what you or others are working on
          right now.
        </p>
      </div>

      <h2>Tips</h2>
      <ul>
        <li>
          Search for <strong>file names</strong> to find sessions that touched
          specific code
        </li>
        <li>
          Search for <strong>error messages</strong> to find how you solved
          similar issues before
        </li>
        <li>
          Search for <strong>tool names</strong> like "Edit" or "Bash" to find
          sessions with specific types of work
        </li>
        <li>
          Use <strong>project filters</strong> to scope results to a single
          codebase
        </li>
      </ul>
    </>
  );
}
