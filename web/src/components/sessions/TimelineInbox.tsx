/**
 * TimelineInbox — inbox-style timeline render.
 *
 * Three tiers: Shelf (open now, flat), Needs attention (unread Console
 * results), then History (all other sessions grouped by project). The history
 * tier keeps open and closed sessions together, ordered by lifecycle activity.
 * See lib/timelineInbox.ts for the pure layout function.
 *
 * Drag-to-reorder: hold and drag any row or project repo header. Threshold-based
 * (5px), so a normal click still navigates. Automation groups stay pinned and
 * are not draggable. Order persists per-browser via localStorage.
 */

import { useCallback, useEffect, useId, useMemo, useState } from "react";
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
import { isSessionClosed } from "../../lib/sessionRuntime";

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
  const inboxId = useId();
  const liveHeadingId = `${inboxId}-live-heading`;
  const attentionHeadingId = `${inboxId}-attention-heading`;
  const historyHeadingId = `${inboxId}-history-heading`;
  const [order, setOrder] = useState<InboxOrderState>(() => readInboxOrder());

  useEffect(() => {
    writeInboxOrder(order);
  }, [order]);

  const layout = useMemo(
    () => buildInboxLayout(sessions, order, relativeNowMs),
    [sessions, order, relativeNowMs],
  );

  const moveShelf = useCallback(
    (from: number, to: number) => {
      if (from === to) return;
      const visibleIds = layout.shelf.map((s) => s.thread_id);
      const reorderedVisible = arrayMove(visibleIds, from, to);
      setOrder((prev) => ({
        ...prev,
        shelfOrder: applyOrder(
          prev.shelfOrder.length ? prev.shelfOrder : visibleIds,
          reorderedVisible,
        ),
      }));
    },
    [layout.shelf],
  );

  const moveRepo = useCallback(
    (from: number, to: number) => {
      if (from === to) return;
      const fromGroup = layout.history[from];
      const toGroup = layout.history[to];
      // Automation groups are pinned below project history and are not
      // reorderable. A project can still move among the other project groups.
      if (!fromGroup || !toGroup || fromGroup.kind === "automation" || toGroup.kind === "automation") {
        return;
      }
      const visibleRepos = layout.history.map((g) => g.repo);
      const reorderedVisible = arrayMove(visibleRepos, from, to);
      setOrder((prev) => ({
        ...prev,
        repoOrder: applyOrder(prev.repoOrder.length ? prev.repoOrder : visibleRepos, reorderedVisible),
      }));
    },
    [layout.history],
  );

  const moveSession = useCallback(
    (repo: string, from: number, to: number) => {
      if (from === to) return;
      const repoGroup = layout.history.find((g) => g.repo === repo);
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
    [layout.history],
  );

  if (
    layout.shelf.length === 0 &&
    layout.unread.length === 0 &&
    layout.history.length === 0
  ) {
    return null;
  }

  return (
    <div className="inbox" data-testid="timeline-inbox">
      {layout.shelf.length > 0 ? (
        <section className="inbox-tier inbox-tier--shelf" aria-labelledby={liveHeadingId}>
          <div className="inbox-live-divider">
            <h2 id={liveHeadingId} className="inbox-live-divider-label">Live now</h2>
            <span className="inbox-live-divider-count">{layout.shelf.length}</span>
          </div>
          <ShelfSection
            sessions={layout.shelf}
            onSessionClick={onSessionClick}
            onSessionPrefetch={onSessionPrefetch}
            allowHoverPrefetch={allowHoverPrefetch}
            relativeNowMs={relativeNowMs}
            highlightQuery={highlightQuery}
            onMoveSession={moveShelf}
          />
        </section>
      ) : null}

      {layout.unread.length > 0 ? (
        <section
          className="inbox-section inbox-section--unread"
          data-testid="timeline-unread"
          aria-labelledby={attentionHeadingId}
        >
          <div className="inbox-unread-divider">
            <h2 id={attentionHeadingId} className="inbox-unread-divider-label">Needs attention</h2>
            <span className="inbox-unread-divider-count">{layout.unread.length}</span>
          </div>
          <div className="inbox-repo-rows">
            {layout.unread.map((thread) => (
              <SessionRow
                key={thread.thread_id}
                thread={thread}
                unread
                onClick={() => onSessionClick(thread)}
                onPrefetch={onSessionPrefetch ? () => onSessionPrefetch(thread) : undefined}
                allowHoverPrefetch={allowHoverPrefetch}
                relativeNowMs={relativeNowMs}
                highlightQuery={highlightQuery}
              />
            ))}
          </div>
        </section>
      ) : null}

      {layout.history.length > 0 ? (
        <section className="inbox-tier inbox-tier--history" aria-labelledby={historyHeadingId}>
          <div className="inbox-history-divider">
            <h2 id={historyHeadingId} className="inbox-history-divider-label">History</h2>
            <span className="inbox-history-divider-count">{layout.historyCount}</span>
          </div>
          <RepoTier
            tier="history"
            groups={layout.history}
            onSessionClick={onSessionClick}
            onSessionPrefetch={onSessionPrefetch}
            allowHoverPrefetch={allowHoverPrefetch}
            relativeNowMs={relativeNowMs}
            highlightQuery={highlightQuery}
            onMoveRepo={moveRepo}
            onMoveSession={moveSession}
          />
        </section>
      ) : null}
    </div>
  );
}

interface ShelfSectionProps {
  sessions: TimelineSessionCard[];
  onSessionClick: (thread: TimelineSessionCard) => void;
  onSessionPrefetch?: (thread: TimelineSessionCard) => void;
  allowHoverPrefetch?: () => boolean;
  relativeNowMs: number;
  highlightQuery?: string;
  onMoveSession: (from: number, to: number) => void;
}

function ShelfSection(props: ShelfSectionProps) {
  const { sessions, onMoveSession } = props;
  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: POINTER_ACTIVATION_DISTANCE } }),
  );

  const ids = sessions.map((s) => `shelf:${s.thread_id}`);

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
    <div className="inbox-section inbox-section--shelf" data-testid="timeline-shelf">
      <DndContext sensors={sensors} collisionDetection={closestCenter} onDragEnd={handleDragEnd}>
        <SortableContext items={ids} strategy={verticalListSortingStrategy}>
          <div className="inbox-repo-rows">
            {sessions.map((thread) => (
              <SortableSessionRow
                key={thread.thread_id}
                id={`shelf:${thread.thread_id}`}
                thread={thread}
                closed={false}
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
    </div>
  );
}

interface RepoTierProps {
  tier: "history";
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
  tier: "history";
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
  const reorderable = group.kind !== "automation";
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } = useSortable({
    id,
    disabled: !reorderable,
  });

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
      data-kind={group.kind}
      data-reorderable={reorderable ? "true" : "false"}
      data-dragging={isDragging ? "true" : undefined}
      aria-label={`${group.label} sessions`}
      style={style}
    >
      <header
        className="inbox-repo-header"
        {...(reorderable ? attributes : {})}
        {...(reorderable ? listeners : {})}
      >
        <div className="inbox-repo-heading">
          <h3 className="inbox-repo-name">{group.label}</h3>
          {group.description ? (
            <span className="inbox-repo-description">{group.description}</span>
          ) : null}
        </div>
        <span className="inbox-repo-count">{group.sessions.length}</span>
      </header>
      <SessionList
        repo={group.repo}
        sessions={group.sessions}
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
  onSessionClick: (thread: TimelineSessionCard) => void;
  onSessionPrefetch?: (thread: TimelineSessionCard) => void;
  allowHoverPrefetch?: () => boolean;
  relativeNowMs: number;
  highlightQuery?: string;
  onMoveSession: (from: number, to: number) => void;
}

function SessionList(props: SessionListProps) {
  const { sessions, repo, onMoveSession } = props;
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
              closed={isSessionClosed(thread.head)}
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
  closed: boolean;
  onSessionClick: (thread: TimelineSessionCard) => void;
  onSessionPrefetch?: (thread: TimelineSessionCard) => void;
  allowHoverPrefetch?: () => boolean;
  relativeNowMs: number;
  highlightQuery?: string;
}

function SortableSessionRow({
  id,
  thread,
  closed,
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
      closed={closed}
      dragging={isDragging}
      style={style}
      sortableAttributes={attributes}
      sortableListeners={listeners}
    />
  );
}
