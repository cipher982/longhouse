import { useEffect, useRef } from "react";

interface PageMetaOptions {
  title: string;
  description?: string;
  restoreOnUnmount?: boolean;
}

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
