import { createContext, useContext, useEffect, useRef } from "react";

interface PageMetaOptions {
  title: string;
  description?: string;
  restoreOnUnmount?: boolean;
}

/**
 * Filled in during prerender (web/scripts/prerender.mjs) so the static HTML for
 * each marketing route carries the same title and description its page sets in
 * the browser. Absent in the browser, where usePageMeta writes to the document.
 */
export interface CollectedPageMeta {
  title?: string;
  description?: string;
}
export const PageMetaCollectorContext = createContext<CollectedPageMeta | null>(null);

function getDescriptionMeta(): HTMLMetaElement | null {
  return document.querySelector('meta[name="description"]');
}

export function usePageMeta({
  title,
  description,
  restoreOnUnmount = true,
}: PageMetaOptions) {
  const previousTitleRef = useRef<string | null>(null);
  const previousDescriptionRef = useRef<string | null>(null);
  const collector = useContext(PageMetaCollectorContext);
  if (collector) {
    collector.title = title;
    collector.description = description;
  }

  useEffect(() => {
    if (previousTitleRef.current === null) {
      previousTitleRef.current = document.title;
    }
    if (previousDescriptionRef.current === null) {
      previousDescriptionRef.current = getDescriptionMeta()?.getAttribute("content") ?? null;
    }

    document.title = title;

    if (description !== undefined) {
      const meta = getDescriptionMeta();
      if (meta) {
        meta.setAttribute("content", description);
      }
    }

    return () => {
      if (!restoreOnUnmount) {
        return;
      }

      if (previousTitleRef.current !== null) {
        document.title = previousTitleRef.current;
      }

      if (description !== undefined) {
        const meta = getDescriptionMeta();
        if (!meta) {
          return;
        }

        if (previousDescriptionRef.current === null) {
          meta.removeAttribute("content");
          return;
        }

        meta.setAttribute("content", previousDescriptionRef.current);
      }
    };
  }, [description, restoreOnUnmount, title]);
}
