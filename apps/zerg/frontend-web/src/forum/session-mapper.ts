/**
 * Maps Longhouse sessions to Forum canvas entities.
 */

import type { ActiveSession } from "../hooks/useActiveSessions";
import type {
  ForumEntity,
  ForumGridPoint,
  ForumMapLayout,
  ForumRoom,
} from "./types";
import { createForumState, type ForumMapState } from "./state";

const ROOM_SIZE = 8; // Grid units per room
const ROOM_PADDING = 3;

function slugify(value: string): string {
  return value
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
}

function truncate(text: string, maxLength: number): string {
  if (text.length <= maxLength) return text;
  return `${text.slice(0, maxLength - 3).trim()}...`;
}

function repoNameFromUrl(url: string | null): string | null {
  if (!url) return null;
  const cleaned = url.replace(/\.git$/, "");
  const parts = cleaned.split("/");
  return parts[parts.length - 1] || null;
}

function cwdBasename(cwd: string | null): string | null {
  if (!cwd) return null;
  const parts = cwd.split("/").filter(Boolean);
  return parts[parts.length - 1] || null;
}

export function getSessionRoomLabel(session: ActiveSession): string {
  return (
    session.project?.trim() ||
    repoNameFromUrl(session.git_repo) ||
    cwdBasename(session.cwd) ||
    "misc"
  );
}

export function getSessionDisplayTitle(session: ActiveSession): string {
  const candidate = (session.last_user_message || session.last_assistant_message || "").trim();
  if (candidate) {
    return truncate(candidate.replace(/\s+/g, " "), 32);
  }
  return (
    session.project?.trim() ||
    repoNameFromUrl(session.git_repo) ||
    cwdBasename(session.cwd) ||
    session.provider
  );
}

function createForumLayout(roomCount: number): ForumMapLayout {
  const roomsPerRow = Math.max(1, Math.ceil(Math.sqrt(roomCount)));
  const rows = Math.max(1, Math.ceil(roomCount / roomsPerRow));
  const cols = Math.max(16, roomsPerRow * (ROOM_SIZE + ROOM_PADDING));
  const gridRows = Math.max(12, rows * (ROOM_SIZE + ROOM_PADDING));

  return {
    id: "layout-main",
    name: "Forum",
    grid: { cols, rows: gridRows },
    tile: { width: 64, height: 32 },
    origin: { x: cols * 18, y: 40 },
  };
}

/**
 * Compute a deterministic position within a room based on session index.
 */
function computePositionInRoom(room: ForumRoom, index: number, total: number): ForumGridPoint {
  const { bounds, center } = room;
  const width = bounds.maxCol - bounds.minCol;
  const height = bounds.maxRow - bounds.minRow;

  const cols = Math.max(1, Math.ceil(Math.sqrt(total)));
  const row = Math.floor(index / cols);
  const col = index % cols;

  const offsetCol = col - Math.floor(cols / 2);
  const offsetRow = row - Math.floor(total / cols / 2);

  return {
    col: Math.max(bounds.minCol + 1, Math.min(bounds.maxCol - 1, center.col + offsetCol * 2)),
    row: Math.max(bounds.minRow + 1, Math.min(bounds.maxRow - 1, center.row + offsetRow * 2)),
  };
}

/**
 * Group sessions by project/repo and create rooms.
 */
export function createRoomsFromSessions(sessions: ActiveSession[]): Map<string, ForumRoom> {
  const projects = new Map<string, string>();
  for (const session of sessions) {
    const label = getSessionRoomLabel(session);
    projects.set(label, label);
  }

  const rooms = new Map<string, ForumRoom>();
  const projectList = Array.from(projects.values()).sort((a, b) => a.localeCompare(b));
  const cols = Math.max(1, Math.ceil(Math.sqrt(projectList.length || 1)));

  projectList.forEach((project, i) => {
    const gridCol = i % cols;
    const gridRow = Math.floor(i / cols);
    const minCol = gridCol * (ROOM_SIZE + ROOM_PADDING);
    const minRow = gridRow * (ROOM_SIZE + ROOM_PADDING);
    const roomId = `room-${slugify(project) || i}`;

    rooms.set(project, {
      id: roomId,
      name: project,
      workspaceId: "workspace-main",
      repoGroupId: "repo-main",
      bounds: {
        minCol,
        minRow,
        maxCol: minCol + ROOM_SIZE,
        maxRow: minRow + ROOM_SIZE,
      },
      center: {
        col: minCol + Math.floor(ROOM_SIZE / 2),
        row: minRow + Math.floor(ROOM_SIZE / 2),
      },
    });
  });

  return rooms;
}

/**
 * Map sessions to canvas entities.
 */
export function mapSessionsToEntities(
  sessions: ActiveSession[],
  rooms: Map<string, ForumRoom>,
): Map<string, ForumEntity> {
  const entities = new Map<string, ForumEntity>();

  const byRoom = new Map<string, ActiveSession[]>();
  for (const session of sessions) {
    const roomKey = getSessionRoomLabel(session);
    if (!byRoom.has(roomKey)) {
      byRoom.set(roomKey, []);
    }
    byRoom.get(roomKey)!.push(session);
  }

  for (const [roomKey, roomSessions] of byRoom) {
    const room = rooms.get(roomKey);
    if (!room) continue;

    const sorted = [...roomSessions].sort(
      (a, b) => new Date(a.started_at).getTime() - new Date(b.started_at).getTime(),
    );

    sorted.forEach((session, index) => {
      const position = computePositionInRoom(room, index, sorted.length);
      const label = getSessionDisplayTitle(session);
      const subtitleParts = [session.provider, session.git_branch].filter(Boolean) as string[];
      const subtitle = subtitleParts.join(" | ");

      entities.set(session.id, {
        id: session.id,
        type: "commis",
        roomId: room.id,
        position,
        status: session.ended_at ? "disabled" : "idle",
        label,
        meta: {
          provider: session.provider,
          room: room.name,
          subtitle: subtitle || undefined,
        },
      });
    });
  }

  return entities;
}

export function buildForumStateFromSessions(sessions: ActiveSession[]): ForumMapState {
  const rooms = createRoomsFromSessions(sessions);
  const layout = createForumLayout(Math.max(rooms.size, 1));
  const base = createForumState({ layout, rooms: Array.from(rooms.values()) });

  base.entities = mapSessionsToEntities(sessions, rooms);
  base.now = Date.now();

  return base;
}
