import { AgentIcon, GlobeIcon, SignalIcon, WrenchIcon, ZapIcon } from "../../components/icons";
import { useStore } from "@xyflow/react";
import type { NodeTypes } from "@xyflow/react";

// Custom node component for agents
export function AgentNode({ data }: { data: { label: string; agentId?: number } }) {
  return (
    <div className="agent-node">
      <div className="agent-icon"><AgentIcon width={20} height={20} /></div>
      <div className="agent-name">{data.label}</div>
    </div>
  );
}

// Custom node component for tools
export function ToolNode({ data }: { data: { label: string; toolType?: string } }) {
  const IconComponent = data.toolType === 'http-request' ? GlobeIcon : data.toolType === 'url-fetch' ? SignalIcon : WrenchIcon;

  return (
    <div className="tool-node">
      <div className="tool-icon"><IconComponent width={20} height={20} /></div>
      <div className="tool-name">{data.label}</div>
    </div>
  );
}

// Custom node component for triggers
export function TriggerNode({ data }: { data: { label: string } }) {
  return (
    <div className="trigger-node">
      <div className="trigger-icon"><ZapIcon width={20} height={20} /></div>
      <div className="trigger-name">{data.label}</div>
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

  return (
    <foreignObject x={x} y={y} width={width} height={height}>
      {/* We use a div with 100% size to contain the node component */}
      <div className="minimap-node-content" style={{ width: '100%', height: '100%' }}>
        {type === 'agent' && <AgentNode data={data as { label: string; agentId?: number }} />}
        {type === 'tool' && <ToolNode data={data as { label: string; toolType?: string }} />}
        {type === 'trigger' && <TriggerNode data={data as { label: string }} />}
      </div>
    </foreignObject>
  );
}

export const nodeTypes: NodeTypes = {
  agent: AgentNode,
  tool: ToolNode,
  trigger: TriggerNode,
};
