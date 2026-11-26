import { useLayoutEffect, useRef } from "react";
import clsx from "clsx";
import { getWindowActivity, subscribeWindowActivity } from "@/shared/hooks/useWindowActivity";
import {
  ACTIVITY_FRAME_WEIGHT,
  ACTIVITY_STRIP_WINDOW_MS,
  type SessionActivityFeed,
} from "./sessionActivityFeed";

export type ActivityStripTone = "live" | "attention" | "idle";

interface ActivityStripProps {
  feed: SessionActivityFeed | null | undefined;
  tone: ActivityStripTone;
  /** CSS pixels; width follows the containing field. */
  height?: number;
  className?: string;
  label?: string;
  title?: string;
  reduceMotion?: boolean;
  showHistory?: boolean;
}

const BAR_WIDTH = 1.5;
const PADDING = 1;
/** 15 fps is visually smooth for a 12-second drift strip and avoids 120 Hz rAF spin. */
const ACTIVITY_STRIP_INTERVAL_MS = 1000 / 15;

function monotonicNow(): number {
  return typeof performance !== "undefined" &&
    typeof performance.now === "function"
    ? performance.now()
    : Date.now();
}

/**
 * A receipt canvas that draws one bar per stream frame and lets bars drift left
 * over a twelve-second window. Nothing loops on its own: the animation frame
 * loop runs only while a bar is still inside the window, so an idle strip
 * costs nothing and a wedged turn visibly flattens.
 */
export function ActivityStrip({
  feed,
  tone,
  height = 14,
  className,
  label,
  title,
  reduceMotion: reduceMotionProp = false,
  showHistory = true,
}: ActivityStripProps) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);

  useLayoutEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    const reduceMotion =
      reduceMotionProp ||
      (typeof window.matchMedia === "function" &&
        window.matchMedia("(prefers-reduced-motion: reduce)").matches);
    const dpr = window.devicePixelRatio || 1;
    let width = canvas.getBoundingClientRect().width;
    const resize = () => {
      width = canvas.getBoundingClientRect().width;
      canvas.width = Math.round(width * dpr);
      canvas.height = Math.round(height * dpr);
      draw();
    };

    const styles = getComputedStyle(canvas);
    const barColor =
      styles.getPropertyValue("--activity-strip-color").trim() ||
      "currentColor";
    const stateColor =
      styles.getPropertyValue("--activity-strip-state-color").trim() ||
      barColor;

    let frameId = 0;
    let nextTimer = 0;
    let expiryTimer = 0;
    let disposed = false;

    const isFocused = () =>
      (typeof document === "undefined" || !document.hidden) &&
      getWindowActivity() === "focused";

    const stopLoop = () => {
      if (nextTimer) {
        window.clearTimeout(nextTimer);
        nextTimer = 0;
      }
      if (frameId) {
        window.cancelAnimationFrame(frameId);
        frameId = 0;
      }
    };

    const draw = (): boolean => {
      if (width <= 0 || height <= 0) return false;
      const now = monotonicNow();
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, width, height);
      const baseY = height - PADDING;

      const frames = showHistory ? (feed?.snapshot() ?? []) : [];
      let visible = false;
      for (let index = frames.length - 1; index >= 0; index -= 1) {
        const frame = frames[index];
        const age = now - frame.at;
        if (age > ACTIVITY_STRIP_WINDOW_MS) break;
        visible = true;
        const progress = age / ACTIVITY_STRIP_WINDOW_MS;
        const x = width - progress * width;
        const barHeight = Math.max(
          2,
          (height - PADDING * 2) * ACTIVITY_FRAME_WEIGHT[frame.kind],
        );
        ctx.globalAlpha = 0.25 + 0.75 * (1 - progress);
        ctx.fillStyle = frame.kind === "state" ? stateColor : barColor;
        ctx.fillRect(x - BAR_WIDTH, baseY - barHeight, BAR_WIDTH, barHeight);
      }
      ctx.globalAlpha = 1;
      return visible;
    };

    const loop = () => {
      frameId = 0;
      if (disposed || !isFocused()) return;
      if (draw()) {
        nextTimer = window.setTimeout(() => {
          nextTimer = 0;
          if (!disposed && !frameId && isFocused()) {
            frameId = window.requestAnimationFrame(loop);
          }
        }, ACTIVITY_STRIP_INTERVAL_MS);
      }
    };

    const wake = () => {
      if (disposed || width <= 0 || height <= 0) return;
      if (!isFocused()) return;
      if (reduceMotion) {
        // No drift, so nothing would otherwise clear a bar once it leaves the
        // window. One deferred repaint after the newest frame expires does it.
        draw();
        window.clearTimeout(expiryTimer);
        expiryTimer = window.setTimeout(() => {
          if (!disposed) draw();
        }, ACTIVITY_STRIP_WINDOW_MS + 50);
        return;
      }
      if (!frameId && !nextTimer) {
        frameId = window.requestAnimationFrame(loop);
      }
    };

    const onActivityChange = () => {
      if (!isFocused()) {
        stopLoop();
        return;
      }
      if (showHistory) wake();
    };

    resize();
    const observer = new ResizeObserver(resize);
    observer.observe(canvas);
    const unsubscribeActivity = subscribeWindowActivity(onActivityChange);
    if (showHistory) wake();
    const unsubscribe = feed && showHistory ? feed.subscribe(wake) : null;

    return () => {
      disposed = true;
      observer.disconnect();
      unsubscribeActivity();
      window.clearTimeout(expiryTimer);
      stopLoop();
      unsubscribe?.();
    };
  }, [feed, tone, height, reduceMotionProp, showHistory]);

  return (
    <canvas
      ref={canvasRef}
      className={clsx(
        "session-activity-strip",
        `session-activity-strip--${tone}`,
        className,
      )}
      style={{ width: "100%", height }}
      role="img"
      aria-label={label}
      title={title}
      data-tone={tone}
      data-testid="session-activity-strip"
    />
  );
}
