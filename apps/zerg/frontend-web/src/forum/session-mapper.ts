/**
 * Maps Life Hub sessions to Forum canvas entities.
 *
 * Converts ActiveSession objects from the API into ForumEntity, ForumTask,
 * and ForumMarker objects for rendering on the canvas.
 */

import type { ActiveSession, AttentionLevel, SessionStatus } from "../hooks/useActiveSessions";
import type {
  ForumEntity,
  ForumEntityStatus,
  ForumGridPoint,
  ForumMarker,
  ForumRoom,
  ForumTask,
  ForumTaskStatus,
} from "./types";

/**
 * Map session status to entity status for canvas rendering.
 */
function mapSessionStatusToEntityStatus(status: SessionStatus): ForumEntityStatus {
  switch (status) {
    case "working":
      return "working";
    case "thinking":
      return "working";
    case "idle":
      return "idle";
    case "completed":
      return "disabled";
    case "active":
      return "moving";
    default:
      return "idle";
  }
}

/**
 * Map session status to task status for the task list.
 */
function mapSessionStatusToTaskStatus(status: SessionStatus, attention: AttentionLevel): ForumTaskStatus {
  if (attention === "hard") return "failed";
  switch (status) {
    case "working":
    case "thinking":
      return "running";
    case "idle":
      return "waiting";
    case "completed":
      return "success";
    case "active":
      return "running";
    default:
      return "queued";
  }
}

/**
 * Compute a deterministic position within a room based on session index.
 * Arranges entities in a grid pattern within the room bounds.
 */
function computePositionInRoom(room: ForumRoom, index: number, total: number): ForumGridPoint {
  const { bounds, center } = room;
  const width = bounds.maxCol - bounds.minCol;
  const height = bounds.maxRow - bounds.minRow;

  // Arrange in a grid around the center
  const cols = Math.max(1, Math.ceil(Math.sqrt(total)));
  const row = Math.floor(index / cols);
  const col = index % cols;

  // Offset from center
  const offsetCol = col - Math.floor(cols / 2);
  const offsetRow = row - Math.floor(total / cols / 2);

  return {
    col: Math.max(bounds.minCol + 1, Math.min(bounds.maxCol - 1, center.col + offsetCol * 2)),
    row: Math.max(bounds.minRow + 1, Math.min(bounds.maxRow - 1, center.row + offsetRow * 2)),
  };
}

/**
 * Group sessions by project and create rooms for each project.
 */
export function createRoomsFromSessions(sessions: ActiveSession[]): Map<string, ForumRoom> {
  const projects = new Set<string>();
  for (const session of sessions) {
    projects.add(session.project || "misc");
  }

  const rooms = new Map<string, ForumRoom>();
  const projectList = Array.from(projects);

  // Arrange rooms in a grid
  const cols = Math.max(1, Math.ceil(Math.sqrt(projectList.length)));
  const roomSize = 8; // Grid units per room
  const padding = 2;

  projectList.forEach((project, i) => {
    const gridCol = i % cols;
    const gridRow = Math.floor(i / cols);
    const minCol = gridCol * (roomSize + padding);
    const minRow = gridRow * (roomSize + padding);

    rooms.set(project, {
      id: `room-${project}`,
      name: project,
      workspaceId: "workspace-main",
      repoGroupId: "repo-main",
      bounds: {
        minCol,
        minRow,
        maxCol: minCol + roomSize,
        maxRow: minRow + roomSize,
      },
      center: {
        col: minCol + Math.floor(roomSize / 2),
        row: minRow + Math.floor(roomSize / 2),
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

  // Group sessions by project for positioning
  const byProject = new Map<string, ActiveSession[]>();
  for (const session of sessions) {
    const project = session.project || "misc";
    if (!byProject.has(project)) {
      byProject.set(project, []);
    }
    byProject.get(project)!.push(session);
  }

  // Create entity for each session
  for (const [project, projectSessions] of byProject) {
    const room = rooms.get(project);
    if (!room) continue;

    projectSessions.forEach((session, index) => {
      const position = computePositionInRoom(room, index, projectSessions.length);

      entities.set(session.id, {
        id: session.id,
        type: "worker",
        roomId: room.id,
        position,
        status: mapSessionStatusToEntityStatus(session.status),
        label: `${session.provider}`,
        meta: {
          provider: session.provider,
          attention: session.attention,
          project: session.project,
          lastMessage: session.last_assistant_message,
        },
      });
    });
  }

  return entities;
}

/**
 * Map sessions to tasks for the task list panel.
 */
export function mapSessionsToTasks(sessions: ActiveSession[], rooms: Map<string, ForumRoom>): Map<string, ForumTask> {
  const tasks = new Map<string, ForumTask>();

  for (const session of sessions) {
    const project = session.project || "misc";
    const room = rooms.get(project);
    if (!room) continue;

    // Compute progress based on status
    let progress = 0;
    if (session.status === "completed") {
      progress = 1;
    } else if (session.status === "working" || session.status === "thinking") {
      progress = 0.5;
    } else if (session.status === "idle") {
      progress = 0.3;
    }

    // Title: project + last message snippet
    const lastMsg = session.last_assistant_message || session.last_user_message || "";
    const title = lastMsg.length > 60 ? `${lastMsg.slice(0, 57)}...` : lastMsg || `${project} session`;

    tasks.set(session.id, {
      id: session.id,
      title,
      status: mapSessionStatusToTaskStatus(session.status, session.attention),
      roomId: room.id,
      entityId: session.id,
      progress,
      createdAt: new Date(session.started_at).getTime(),
      updatedAt: new Date(session.last_activity_at).getTime(),
    });
  }

  return tasks;
}

/**
 * Create attention markers for sessions that need attention.
 */
export function createAttentionMarkers(
  sessions: ActiveSession[],
  entities: Map<string, ForumEntity>,
): Map<string, ForumMarker> {
  const markers = new Map<string, ForumMarker>();
  const now = Date.now();

  for (const session of sessions) {
    if (session.attention !== "hard" && session.attention !== "needs") {
      continue;
    }

    const entity = entities.get(session.id);
    if (!entity) continue;

    markers.set(`attention-${session.id}`, {
      id: `attention-${session.id}`,
      type: session.attention === "hard" ? "ping" : "focus",
      roomId: entity.roomId,
      position: entity.position,
      label: session.attention === "hard" ? "!" : "?",
      createdAt: now,
      expiresAt: now + 60000, // 1 minute
    });
  }

  return markers;
}
