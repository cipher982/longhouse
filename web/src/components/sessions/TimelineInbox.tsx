/**
 * TimelineInbox — inbox-style timeline render.
 *
 * Two tiers (Active, then Closed), each grouped by repo. Repo order =
 * newest session start desc. Sessions inside a repo = start time desc.
 * Layout is anchored to start time so live runtime updates never reflow
 * the page. See lib/timelineInbox.ts for the pure layout function.
 *
 * Drag-to-reorder: hold and drag any row or any repo header. Threshold-
 * based (5px), so a normal click still navigates. Live preview via dnd-kit
 * — rows physically follow the cursor and siblings animate aside. Order
 * persists per-browser via localStorage. New sessions/repos slot to the
 * top (default order).
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  DndContext,
  PointerSensor,
  useSensor,
  useSensors,
  closestCenter,
  type DragEndEvent,
} from "@dnd-kit/core";
import {
  SortableContext,
  arrayMove,
  useSortable,
  verticalListSortingStrategy,
} from "@dnd-kit/sortable";
import { CSS } from "@dnd-kit/utilities";

import { type TimelineSessionCard } from "../../services/api/agents";
import { buildInboxLayout, type InboxRepoGroup } from "../../lib/timelineInbox";
import {
  applyOrder,
  readInboxOrder,
  writeInboxOrder,
  type InboxOrderState,
} from "../../lib/inboxOrder";
import { SessionRow } from "./SessionRow";

const POINTER_ACTIVATION_DISTANCE = 5;

export interface TimelineInboxProps {
  sessions: TimelineSessionCard[];
  onSessionClick: (thread: TimelineSessionCard) => void;
  onSessionPrefetch?: (thread: TimelineSessionCard) => void;
  allowHoverPrefetch?: () => boolean;
  relativeNowMs: number;
  highlightQuery?: string;
}

export function TimelineInbox({
  sessions,
  onSessionClick,
  onSessionPrefetch,
  allowHoverPrefetch,
  relativeNowMs,
  highlightQuery,
}: TimelineInboxProps) {
  const [order, setOrder] = useState<InboxOrderState>(() => readInboxOrder());

  useEffect(() => {
    writeInboxOrder(order);
  }, [order]);

  const layout = useMemo(() => buildInboxLayout(sessions, order), [sessions, order]);

  const moveRepo = useCallback(
    (tier: "active" | "closed", from: number, to: number) => {
      if (from === to) return;
      const visibleRepos = (tier === "active" ? layout.active : layout.closed).map((g) => g.repo);
      const reorderedVisible = arrayMove(visibleRepos, from, to);
      setOrder((prev) => ({
        ...prev,
        repoOrder: applyOrder(prev.repoOrder.length ? prev.repoOrder : visibleRepos, reorderedVisible),
      }));
    },
    [layout.active, layout.closed],
  );

  const moveSession = useCallback(
    (tier: "active" | "closed", repo: string, from: number, to: number) => {
      if (from === to) return;
      const tierGroups = tier === "active" ? layout.active : layout.closed;
      const repoGroup = tierGroups.find((g) => g.repo === repo);
      if (!repoGroup) return;
      const visibleIds = repoGroup.sessions.map((s) => s.thread_id);
      const reorderedVisible = arrayMove(visibleIds, from, to);
      setOrder((prev) => ({
        ...prev,
        sessionOrder: {
          ...prev.sessionOrder,
          [repo]: applyOrder(prev.sessionOrder[repo]?.length ? prev.sessionOrder[repo] : visibleIds, reorderedVisible),
        },
      }));
    },
    [layout.active, layout.closed],
  );

  if (layout.active.length === 0 && layout.closed.length === 0) {
    return null;
  }

  return (
    <div className="inbox" data-testid="timeline-inbox">
      {layout.active.length > 0 ? (
        <RepoTier
          tier="active"
          groups={layout.active}
          onSessionClick={onSessionClick}
          onSessionPrefetch={onSessionPrefetch}
          allowHoverPrefetch={allowHoverPrefetch}
          relativeNowMs={relativeNowMs}
          highlightQuery={highlightQuery}
          onMoveRepo={(from, to) => moveRepo("active", from, to)}
          onMoveSession={(repo, from, to) => moveSession("active", repo, from, to)}
        />
      ) : null}

      {layout.closed.length > 0 ? (
        <>
          <div className="inbox-closed-divider" role="separator">
            <span className="inbox-closed-divider-label">Closed</span>
            <span className="inbox-closed-divider-count">{layout.closedCount}</span>
          </div>
          <RepoTier
            tier="closed"
            groups={layout.closed}
            onSessionClick={onSessionClick}
            onSessionPrefetch={onSessionPrefetch}
            allowHoverPrefetch={allowHoverPrefetch}
            relativeNowMs={relativeNowMs}
            highlightQuery={highlightQuery}
            onMoveRepo={(from, to) => moveRepo("closed", from, to)}
            onMoveSession={(repo, from, to) => moveSession("closed", repo, from, to)}
          />
        </>
      ) : null}
    </div>
  );
}

interface RepoTierProps {
  tier: "active" | "closed";
  groups: InboxRepoGroup[];
  onSessionClick: (thread: TimelineSessionCard) => void;
  onSessionPrefetch?: (thread: TimelineSessionCard) => void;
  allowHoverPrefetch?: () => boolean;
  relativeNowMs: number;
  highlightQuery?: string;
  onMoveRepo: (from: number, to: number) => void;
  onMoveSession: (repo: string, from: number, to: number) => void;
}

function RepoTier(props: RepoTierProps) {
  const { groups, tier, onMoveRepo } = props;
  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: POINTER_ACTIVATION_DISTANCE } }),
  );

  const ids = groups.map((g) => `repo:${tier}:${g.repo}`);

  const handleDragEnd = useCallback(
    (event: DragEndEvent) => {
      const { active, over } = event;
      if (!over || active.id === over.id) return;
      const from = ids.indexOf(String(active.id));
      const to = ids.indexOf(String(over.id));
      if (from < 0 || to < 0) return;
      onMoveRepo(from, to);
    },
    [ids, onMoveRepo],
  );

  return (
    <div className={`inbox-section inbox-section--${tier}`}>
      <DndContext sensors={sensors} collisionDetection={closestCenter} onDragEnd={handleDragEnd}>
        <SortableContext items={ids} strategy={verticalListSortingStrategy}>
          {groups.map((group) => (
            <SortableRepoBlock
              key={`${tier}:${group.repo}`}
              id={`repo:${tier}:${group.repo}`}
              group={group}
              tier={tier}
              onSessionClick={props.onSessionClick}
              onSessionPrefetch={props.onSessionPrefetch}
              allowHoverPrefetch={props.allowHoverPrefetch}
              relativeNowMs={props.relativeNowMs}
              highlightQuery={props.highlightQuery}
              onMoveSession={props.onMoveSession}
            />
          ))}
        </SortableContext>
      </DndContext>
    </div>
  );
}

interface SortableRepoBlockProps {
  id: string;
  group: InboxRepoGroup;
  tier: "active" | "closed";
  onSessionClick: (thread: TimelineSessionCard) => void;
  onSessionPrefetch?: (thread: TimelineSessionCard) => void;
  allowHoverPrefetch?: () => boolean;
  relativeNowMs: number;
  highlightQuery?: string;
  onMoveSession: (repo: string, from: number, to: number) => void;
}

function SortableRepoBlock({
  id,
  group,
  tier,
  onSessionClick,
  onSessionPrefetch,
  allowHoverPrefetch,
  relativeNowMs,
  highlightQuery,
  onMoveSession,
}: SortableRepoBlockProps) {
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } = useSortable({ id });

  const style: React.CSSProperties = {
    transform: CSS.Transform.toString(transform),
    transition,
  };

  return (
    <section
      ref={setNodeRef}
      className="inbox-repo"
      data-tier={tier}
      data-repo={group.repo}
      data-dragging={isDragging ? "true" : undefined}
      aria-label={`${group.repo} sessions`}
      style={style}
    >
      <header className="inbox-repo-header" {...attributes} {...listeners}>
        <h2 className="inbox-repo-name">{group.repo}</h2>
        <span className="inbox-repo-count">{group.sessions.length}</span>
      </header>
      <SessionList
        repo={group.repo}
        sessions={group.sessions}
        tier={tier}
        onSessionClick={onSessionClick}
        onSessionPrefetch={onSessionPrefetch}
        allowHoverPrefetch={allowHoverPrefetch}
        relativeNowMs={relativeNowMs}
        highlightQuery={highlightQuery}
        onMoveSession={(from, to) => onMoveSession(group.repo, from, to)}
      />
    </section>
  );
}

interface SessionListProps {
  repo: string;
  sessions: TimelineSessionCard[];
  tier: "active" | "closed";
  onSessionClick: (thread: TimelineSessionCard) => void;
  onSessionPrefetch?: (thread: TimelineSessionCard) => void;
  allowHoverPrefetch?: () => boolean;
  relativeNowMs: number;
  highlightQuery?: string;
  onMoveSession: (from: number, to: number) => void;
}

function SessionList(props: SessionListProps) {
  const { sessions, repo, tier, onMoveSession } = props;
  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: POINTER_ACTIVATION_DISTANCE } }),
  );

  const ids = sessions.map((s) => `session:${repo}:${s.thread_id}`);

  const handleDragEnd = useCallback(
    (event: DragEndEvent) => {
      const { active, over } = event;
      if (!over || active.id === over.id) return;
      const from = ids.indexOf(String(active.id));
      const to = ids.indexOf(String(over.id));
      if (from < 0 || to < 0) return;
      onMoveSession(from, to);
    },
    [ids, onMoveSession],
  );

  return (
    <DndContext sensors={sensors} collisionDetection={closestCenter} onDragEnd={handleDragEnd}>
      <SortableContext items={ids} strategy={verticalListSortingStrategy}>
        <div className="inbox-repo-rows">
          {sessions.map((thread) => (
            <SortableSessionRow
              key={thread.thread_id}
              id={`session:${repo}:${thread.thread_id}`}
              thread={thread}
              tier={tier}
              onSessionClick={props.onSessionClick}
              onSessionPrefetch={props.onSessionPrefetch}
              allowHoverPrefetch={props.allowHoverPrefetch}
              relativeNowMs={props.relativeNowMs}
              highlightQuery={props.highlightQuery}
            />
          ))}
        </div>
      </SortableContext>
    </DndContext>
  );
}

interface SortableSessionRowProps {
  id: string;
  thread: TimelineSessionCard;
  tier: "active" | "closed";
  onSessionClick: (thread: TimelineSessionCard) => void;
  onSessionPrefetch?: (thread: TimelineSessionCard) => void;
  allowHoverPrefetch?: () => boolean;
  relativeNowMs: number;
  highlightQuery?: string;
}

function SortableSessionRow({
  id,
  thread,
  tier,
  onSessionClick,
  onSessionPrefetch,
  allowHoverPrefetch,
  relativeNowMs,
  highlightQuery,
}: SortableSessionRowProps) {
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } = useSortable({ id });

  const style: React.CSSProperties = {
    transform: CSS.Transform.toString(transform),
    transition,
  };

  return (
    <SessionRow
      forwardedRef={setNodeRef}
      thread={thread}
      onClick={() => onSessionClick(thread)}
      onPrefetch={onSessionPrefetch ? () => onSessionPrefetch(thread) : undefined}
      allowHoverPrefetch={allowHoverPrefetch}
      relativeNowMs={relativeNowMs}
      highlightQuery={highlightQuery}
      closed={tier === "closed"}
      dragging={isDragging}
      style={style}
      sortableAttributes={attributes}
      sortableListeners={listeners}
    />
  );
}
