import { Link } from "react-router-dom";
import { BlogHeader } from "../components/blog/BlogHeader";
import { usePageMeta } from "../hooks/usePageMeta";
import { usePublicPageScroll } from "../hooks/usePublicPageScroll";
import { useRootUiEffects } from "../hooks/useRootUiEffects";
import "../styles/blog.css";

export default function BlogIndexPage() {
  usePublicPageScroll();
  useRootUiEffects(false);
  usePageMeta({
    title: "Blog - Longhouse",
    description: "Technical notes about Longhouse session ingest and provider-native control paths.",
  });

  return (
    <div className="blog-page">
      <BlogHeader />
      <main className="blog-index-main">
        <header className="blog-index-hero">
          <p className="blog-eyebrow">Longhouse blog</p>
          <h1>Technical notes on session control.</h1>
          <p>Longhouse implementation notes, provider integrations, and machine-facing product design.</p>
        </header>

        <section className="blog-index-list" aria-label="Posts">
          <Link className="blog-post-card" to="/blog/provider-integrations">
            <div className="blog-post-card-meta">
              <span>Guest post</span>
              <span aria-hidden="true">·</span>
              <time dateTime="2026-07-18">July 18, 2026</time>
            </div>
            <h2>Longhouse Provider Integrations</h2>
            <p>Parsing and managed-control paths for Claude Code, Codex, OpenCode, Antigravity, and Cursor.</p>
            <span className="blog-post-card-link">Read post <span aria-hidden="true">→</span></span>
          </Link>
        </section>
      </main>
    </div>
  );
}
