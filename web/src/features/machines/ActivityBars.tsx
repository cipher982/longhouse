import type { MachineActivity } from "@/shared/api/index";
import { getProviderLabel } from "@/shared/lib/providers";
import { activityColor } from "./machinePresentation";

/**
 * Sessions started per day, stacked by provider in its chart colour. Days
 * with nothing show a hairline stub so an empty fortnight still reads as a
 * fortnight rather than as missing data.
 */
export function ActivityBars({
  daily,
  height,
  size = "mini",
}: {
  daily: MachineActivity["daily"];
  height: number;
  size?: "mini" | "full";
}) {
  const max = Math.max(1, ...daily.map((day) => day.total));
  return (
    <div
      className={`machine-bars machine-bars--${size}`}
      style={{ height }}
      role="img"
      aria-label={`${daily.reduce((sum, day) => sum + day.total, 0)} sessions over ${daily.length} days`}
    >
      {daily.map((day) => {
        const entries = Object.entries(day.by_provider ?? {}).sort((a, b) => b[1] - a[1]);
        const title = day.total
          ? `${day.date}: ${entries.map(([provider, count]) => `${count} ${getProviderLabel(provider)}`).join(", ")}`
          : `${day.date}: no sessions`;
        return (
          <span
            key={day.date}
            className={day.total ? "machine-bar" : "machine-bar machine-bar--empty"}
            style={day.total ? { height: Math.max(3, Math.round((day.total / max) * height)) } : undefined}
            title={title}
          >
            {entries.map(([provider, count]) => (
              <i key={provider} style={{ flexGrow: count, background: activityColor(provider) }} />
            ))}
          </span>
        );
      })}
    </div>
  );
}
