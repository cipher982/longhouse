import { useEffect, useState } from "react";

export function LandingPerfHud({
  fxText,
}: {
  fxText: string;
}) {
  const [stats, setStats] = useState<{ fps: number; avgMs: number; p95Ms: number } | null>(null);

  useEffect(() => {
    const frameTimes: number[] = [];
    let last = performance.now();
    let rafId = 0;
    let intervalId = 0;

    const onFrame = (now: number) => {
      const dt = now - last;
      last = now;
      frameTimes.push(dt);
      if (frameTimes.length > 240) {
        frameTimes.shift();
      }
      rafId = requestAnimationFrame(onFrame);
    };

    rafId = requestAnimationFrame(onFrame);

    intervalId = window.setInterval(() => {
      if (frameTimes.length < 5) {
        return;
      }
      const times = [...frameTimes].sort((a, b) => a - b);
      const avgMs = times.reduce((sum, value) => sum + value, 0) / times.length;
      const p95Ms = times[Math.floor(times.length * 0.95)] ?? avgMs;
      const fps = avgMs > 0 ? 1000 / avgMs : 0;
      setStats({ fps, avgMs, p95Ms });
    }, 500);

    return () => {
      cancelAnimationFrame(rafId);
      window.clearInterval(intervalId);
    };
  }, []);

  if (!stats) {
    return null;
  }

  return (
    <div className="landing-perf-hud">
      <div>{`fps ~ ${stats.fps.toFixed(0)}`}</div>
      <div>{`avg ${stats.avgMs.toFixed(1)}ms`}</div>
      <div>{`p95 ${stats.p95Ms.toFixed(1)}ms`}</div>
      <div className="landing-perf-hud-fx">{`fx: ${fxText}`}</div>
    </div>
  );
}
