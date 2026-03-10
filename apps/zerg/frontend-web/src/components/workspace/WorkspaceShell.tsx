import type { ReactNode } from "react";

interface WorkspaceShellProps {
  header: ReactNode;
  sidebar: ReactNode;
  main: ReactNode;
  inspector?: ReactNode;
  bottom?: ReactNode;
}

export function WorkspaceShell({
  header,
  sidebar,
  main,
  inspector,
  bottom,
}: WorkspaceShellProps) {
  const hasInspector = Boolean(inspector);

  return (
    <div className={`workspace-shell${hasInspector ? "" : " workspace-shell--inspector-collapsed"}`}>
      <div className="workspace-shell__header">{header}</div>
      <div className="workspace-shell__body">
        <aside className="workspace-shell__pane workspace-shell__pane--sidebar">{sidebar}</aside>
        <main className="workspace-shell__pane workspace-shell__pane--main">{main}</main>
        {hasInspector ? (
          <aside className="workspace-shell__pane workspace-shell__pane--inspector">{inspector}</aside>
        ) : null}
      </div>
      {bottom ? <div className="workspace-shell__bottom">{bottom}</div> : null}
    </div>
  );
}
