type UmamiLike = {
  track?: (eventName: string, eventData?: Record<string, string | number | boolean | null>) => void;
};

declare global {
  interface Window {
    umami?: UmamiLike;
  }
}

export type AcquisitionEventProps = Record<string, string | number | boolean | null | undefined>;

export function trackAcquisitionEvent(eventName: string, props: AcquisitionEventProps = {}): void {
  if (typeof window === "undefined") return;

  const cleaned: Record<string, string | number | boolean | null> = {};
  for (const [key, value] of Object.entries(props)) {
    if (value !== undefined) cleaned[key] = value;
  }

  try {
    window.umami?.track?.(eventName, cleaned);
  } catch {
    // Analytics must never interfere with the product funnel.
  }
}
