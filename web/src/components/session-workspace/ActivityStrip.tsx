import { useEffect, useRef } from "react";
import clsx from "clsx";
import {
  ACTIVITY_FRAME_WEIGHT,
  ACTIVITY_STRIP_WINDOW_MS,
  type SessionActivityFeed,
} from "../../lib/sessionActivityFeed";

export type ActivityStripTone = "live" | "attention" | "idle";

interface ActivityStripProps {
  feed: SessionActivityFeed | null | undefined;
  tone: ActivityStripTone;
  /** CSS pixels. */
  width?: number;
  height?: number;
  className?: string;
  label?: string;
  title?: string;
}

const BAR_WIDTH = 1.5;
const PADDING = 1;

function monotonicNow(): number {
  return typeof performance !== "undefined" && typeof performance.now === "function"
    ? performance.now()
    : Date.now();
}

/**
 * A small canvas that draws one bar per stream frame and lets bars drift left
 * over a twelve-second window. Nothing loops on its own: the animation frame
 * loop runs only while a bar is still inside the window, so an idle strip
 * costs nothing and a wedged turn visibly flattens.
 */
export function ActivityStrip({
  feed,
  tone,
  width = 28,
  height = 14,
  className,
  label,
  title,
}: ActivityStripProps) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    const reduceMotion =
      typeof window.matchMedia === "function" &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const dpr = window.devicePixelRatio || 1;
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(height * dpr);

    const styles = getComputedStyle(canvas);
    const barColor = styles.getPropertyValue("--activity-strip-color").trim() || "currentColor";
    const stateColor =
      styles.getPropertyValue("--activity-strip-state-color").trim() || barColor;

    let frameId = 0;
    let expiryTimer = 0;
    let disposed = false;

    const draw = (): boolean => {
      const now = monotonicNow();
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, width, height);
      const baseY = height - PADDING;
      ctx.globalAlpha = 0.28;
      ctx.fillStyle = barColor;
      ctx.fillRect(0, baseY - 0.5, width, 1);

      const frames = feed?.snapshot() ?? [];
      let visible = false;
      for (let index = frames.length - 1; index >= 0; index -= 1) {
        const frame = frames[index];
        const age = now - frame.at;
        if (age > ACTIVITY_STRIP_WINDOW_MS) break;
        visible = true;
        const progress = age / ACTIVITY_STRIP_WINDOW_MS;
        const x = width - progress * width;
        const barHeight = Math.max(2, (height - PADDING * 2) * ACTIVITY_FRAME_WEIGHT[frame.kind]);
        ctx.globalAlpha = 0.25 + 0.75 * (1 - progress);
        ctx.fillStyle = frame.kind === "state" ? stateColor : barColor;
        ctx.fillRect(x - BAR_WIDTH, baseY - barHeight, BAR_WIDTH, barHeight);
      }
      ctx.globalAlpha = 1;
      return visible;
    };

    const loop = () => {
      frameId = 0;
      if (disposed) return;
      if (draw()) {
        frameId = window.requestAnimationFrame(loop);
      }
    };

    const wake = () => {
      if (disposed) return;
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
      if (!frameId) {
        frameId = window.requestAnimationFrame(loop);
      }
    };

    wake();
    const unsubscribe = feed ? feed.subscribe(wake) : null;

    return () => {
      disposed = true;
      window.clearTimeout(expiryTimer);
      if (frameId) {
        window.cancelAnimationFrame(frameId);
        frameId = 0;
      }
      unsubscribe?.();
    };
  }, [feed, tone, width, height]);

  return (
    <canvas
      ref={canvasRef}
      className={clsx("session-activity-strip", `session-activity-strip--${tone}`, className)}
      style={{ width, height }}
      role="img"
      aria-label={label}
      title={title}
      data-tone={tone}
      data-testid="session-activity-strip"
    />
  );
}
