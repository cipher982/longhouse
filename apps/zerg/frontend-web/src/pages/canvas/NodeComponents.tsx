import { getNodeIcon } from "../../lib/iconUtils";
import { Handle, Position, useStore } from "@xyflow/react";
import type { NodeTypes } from "@xyflow/react";

// Custom node component for fiches
export function FicheNode({ data }: { data: { label: string; ficheId?: number } }) {
  return (
    <div className="fiche-node">
      <Handle type="target" position={Position.Left} />
      <div className="fiche-icon">{getNodeIcon("fiche")}</div>
      <div className="fiche-name">{data.label}</div>
      <Handle type="source" position={Position.Right} />
    </div>
  );
}

// Custom node component for tools
export function ToolNode({ data }: { data: { label: string; toolType?: string } }) {
  return (
    <div className="tool-node">
      <Handle type="target" position={Position.Left} />
      <div className="tool-icon">{getNodeIcon("tool", data.toolType)}</div>
      <div className="tool-name">{data.label}</div>
      <Handle type="source" position={Position.Right} />
    </div>
  );
}

// Custom node component for triggers (source only - triggers start workflows)
export function TriggerNode({ data }: { data: { label: string } }) {
  return (
    <div className="trigger-node">
      <div className="trigger-icon">{getNodeIcon("trigger")}</div>
      <div className="trigger-name">{data.label}</div>
      <Handle type="source" position={Position.Right} />
    </div>
  );
}

// Simplified node renderers for MiniMap (no handles - avoids React Flow warnings)
function MiniMapFicheNode({ label }: { label: string }) {
  return (
    <div className="fiche-node">
      <div className="fiche-icon">{getNodeIcon("fiche")}</div>
      <div className="fiche-name">{label}</div>
    </div>
  );
}

function MiniMapToolNode({ label }: { label: string }) {
  return (
    <div className="tool-node">
      <div className="tool-icon">{getNodeIcon("tool")}</div>
      <div className="tool-name">{label}</div>
    </div>
  );
}

function MiniMapTriggerNode({ label }: { label: string }) {
  return (
    <div className="trigger-node">
      <div className="trigger-icon">{getNodeIcon("trigger")}</div>
      <div className="trigger-name">{label}</div>
    </div>
  );
}

// Custom node component for the MiniMap
// Uses foreignObject to render the actual node content (scaled down)
export function MiniMapNode(props: { x: number; y: number; width: number; height: number; id: string }) {
  // Extract positioning and dimensions
  const { x, y, width, height, id } = props;

  // Retrieve the full node data from the store using the ID
  // properties like 'type' and 'data' are not passed directly to MiniMapNode in all versions
  const node = useStore((s) => s.nodeLookup.get(id));

  if (!node) return null;

  const { type, data } = node;
  const nodeData = data as { label: string };

  return (
    <foreignObject x={x} y={y} width={width} height={height}>
      {/* We use a div with 100% size to contain the node component */}
      <div className="minimap-node-content" style={{ width: '100%', height: '100%' }}>
        {type === 'fiche' && <MiniMapFicheNode label={nodeData.label} />}
        {type === 'tool' && <MiniMapToolNode label={nodeData.label} />}
        {type === 'trigger' && <MiniMapTriggerNode label={nodeData.label} />}
      </div>
    </foreignObject>
  );
}

export const nodeTypes: NodeTypes = {
  fiche: FicheNode,
  tool: ToolNode,
  trigger: TriggerNode,
};
