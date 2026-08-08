import { Player, type PlayerRef } from "@remotion/player";
import {
  ControlRoom,
  controlRoomDuration,
  FPS,
  HEIGHT,
  WIDTH,
} from "@longhouse/video";
import { useEffect, useRef, useState } from "react";

const POSTER_FRAME = 330;

function usePrefersReducedMotion(): boolean {
  const [prefersReducedMotion, setPrefersReducedMotion] = useState(() =>
    typeof window !== "undefined" && window.matchMedia
      ? window.matchMedia("(prefers-reduced-motion: reduce)").matches
      : false,
  );

  useEffect(() => {
    const mediaQuery = window.matchMedia("(prefers-reduced-motion: reduce)");
    const update = () => setPrefersReducedMotion(mediaQuery.matches);
    update();
    mediaQuery.addEventListener("change", update);
    return () => mediaQuery.removeEventListener("change", update);
  }, []);

  return prefersReducedMotion;
}

export function LiveDemo({
  "aria-label": ariaLabel,
}: {
  "aria-label": string;
}) {
  const playerRef = useRef<PlayerRef>(null);
  const wrapperRef = useRef<HTMLDivElement>(null);
  const prefersReducedMotion = usePrefersReducedMotion();
  const [isInViewport, setIsInViewport] = useState(true);
  const [isDocumentVisible, setIsDocumentVisible] = useState(
    () =>
      typeof document === "undefined" || document.visibilityState !== "hidden",
  );
  const lastFrameRef = useRef(0);
  const shouldPlay = !prefersReducedMotion && isInViewport && isDocumentVisible;

  useEffect(() => {
    const wrapper = wrapperRef.current;
    if (!wrapper || typeof IntersectionObserver === "undefined") return;

    const observer = new IntersectionObserver(([entry]) => {
      setIsInViewport(entry?.isIntersecting ?? false);
    });
    observer.observe(wrapper);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    const updateVisibility = () =>
      setIsDocumentVisible(document.visibilityState !== "hidden");
    document.addEventListener("visibilitychange", updateVisibility);
    return () =>
      document.removeEventListener("visibilitychange", updateVisibility);
  }, []);

  useEffect(() => {
    const player = playerRef.current;
    if (!player) return;

    if (!shouldPlay) {
      player.pause();
      lastFrameRef.current = prefersReducedMotion
        ? POSTER_FRAME
        : player.getCurrentFrame();
      if (prefersReducedMotion) player.seekTo(POSTER_FRAME);
      return;
    }

    player.seekTo(lastFrameRef.current);
    player.play();
  }, [prefersReducedMotion, shouldPlay]);

  return (
    <div ref={wrapperRef} className="landing-live-demo" aria-label={ariaLabel}>
      <Player
        ref={playerRef}
        className="landing-live-demo-player"
        component={ControlRoom}
        durationInFrames={controlRoomDuration()}
        fps={FPS}
        compositionWidth={WIDTH}
        compositionHeight={HEIGHT}
        loop
        controls={false}
        autoPlay={shouldPlay}
        initialFrame={prefersReducedMotion ? POSTER_FRAME : 0}
      />
    </div>
  );
}
