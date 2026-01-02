import { useEffect } from "react";

/**
 * Hook to enable document scrolling on public pages.
 *
 * The app-shell layout uses `#react-root { overflow: hidden }` which prevents
 * document scrolling. Public pages (landing, pricing, docs, etc.) need native
 * scrolling, so this hook adds a class to html/body that overrides the root
 * container styles.
 */
export function usePublicPageScroll() {
  useEffect(() => {
    document.documentElement.classList.add("public-page-scroll");
    document.body.classList.add("public-page-scroll");

    return () => {
      document.documentElement.classList.remove("public-page-scroll");
      document.body.classList.remove("public-page-scroll");
    };
  }, []);
}
