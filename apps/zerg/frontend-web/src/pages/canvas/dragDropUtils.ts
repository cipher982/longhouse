export type DropPayload =
  | { type: "fiche"; ficheId: number; label: string }
  | { type: "tool"; toolType: string; label: string };

export type DragPreviewKind = "fiche" | "tool";

export interface DragPreviewData {
  kind: DragPreviewKind;
  label: string;
  icon: string;
  baseSize: { width: number; height: number };
  pointerRatio: { x: number; y: number };
  ficheId?: number;
  toolType?: string;
}

export const clamp = (value: number, min: number, max: number) => Math.min(Math.max(value, min), max);

export function toDropPayload(
  raw: { type: "fiche" | "tool"; id?: string; name: string; tool_type?: string }
): DropPayload | null {
  if (!raw?.type || !raw.name) {
    return null;
  }

  if (raw.type === "fiche") {
    const ficheId = parseInt(raw.id ?? "", 10);
    if (!ficheId) {
      return null;
    }
    return { type: "fiche", ficheId, label: raw.name };
  }

  if (raw.type === "tool") {
    if (typeof raw.tool_type !== "string" || raw.tool_type.length === 0) {
      return null;
    }
    return { type: "tool", toolType: raw.tool_type, label: raw.name };
  }

  return null;
}

export function createTransparentDragImage(): HTMLImageElement {
  const img = new Image();
  img.src = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==";
  return img;
}
