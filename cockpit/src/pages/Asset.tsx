import { useMemo } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { Background, Controls, MarkerType, ReactFlow, type Edge, type Node } from "@xyflow/react";
import { ArrowLeft, FolderOpen, GitFork, ListTree, Radio } from "lucide-react";
import { usePoll } from "../hooks";
import { humanize, show } from "../format";
import { useEffectiveTheme } from "../useEffectiveTheme";
import type { Asset, AssetView } from "../types";
import { Empty, ErrorNote, KeyValues, Loading, Panel, SeverityChip, StatusPill, Tag, Time } from "../components/ui";

const LAYERS: string[][] = [
  ["service", "application", "m365_service"],
  ["database", "sql_instance", "sql_agent_job", "backup_job"],
  ["server", "endpoint", "cloud_resource"],
  ["hypervisor_host", "hypervisor_cluster", "network_device", "device"],
  ["datastore", "backup_repository", "storage_array", "power"],
];
const layerOf = (kind: string) => {
  const i = LAYERS.findIndex((l) => l.includes(kind));
  return i >= 0 ? i : 2;
};

const NODE_W = 176;
const GAP_X = 28;
const GAP_Y = 120;
const MAX_PER_ROW = 5;

function layout(assets: Asset[], centre: string, relations: { src: string; rel: string; dst: string }[]) {
  const layers: Asset[][] = LAYERS.map(() => []);
  for (const a of assets) layers[layerOf(a.kind)].push(a);
  // Keep the centre and its direct neighbours near the middle of each row.
  const near = new Set<string>([centre]);
  for (const r of relations) {
    if (r.src === centre) near.add(r.dst);
    if (r.dst === centre) near.add(r.src);
  }
  const used = layers.filter((l) => l.length > 0);
  // Wide layers wrap onto several rows so the graph stays readable at fit-view zoom.
  const rows: Asset[][] = [];
  for (const layer of used) {
    layer.sort((a, b) => Number(near.has(b.id)) - Number(near.has(a.id)) || a.kind.localeCompare(b.kind) || a.name.localeCompare(b.name));
    for (let i = 0; i < layer.length; i += MAX_PER_ROW) rows.push(layer.slice(i, i + MAX_PER_ROW));
  }
  const widest = Math.max(1, ...rows.map((l) => l.length));
  const nodes: Node[] = [];
  let y = 0;
  rows.forEach((row, ri) => {
    // centre-out ordering: [.., 3, 1, 0, 2, 4, ..]
    const ordered: Asset[] = [];
    row.forEach((a, i) => (i % 2 === 0 ? ordered.push(a) : ordered.unshift(a)));
    const offset = ((widest - ordered.length) * (NODE_W + GAP_X)) / 2;
    const sameLayerAsPrev = ri > 0 && layerOf(rows[ri - 1][0].kind) === layerOf(row[0].kind);
    if (ri > 0) y += sameLayerAsPrev ? GAP_Y * 0.62 : GAP_Y;
    ordered.forEach((a, x) => {
      const isCentre = a.id === centre;
      nodes.push({
        id: a.id,
        position: { x: offset + x * (NODE_W + GAP_X), y },
        data: {
          label: (
            <div className="gnode-inner">
              <div className="gnode-kind">{humanize(a.kind)}</div>
              <div className="gnode-name">{a.name}</div>
            </div>
          ),
        },
        className: `gnode ${isCentre ? "centre" : ""} ${a.attrs.critical === true ? "critical" : ""}`,
        style: { width: NODE_W },
        draggable: true,
        connectable: false,
      });
    });
  });
  const ids = new Set(assets.map((a) => a.id));
  const edges: Edge[] = relations
    .filter((r) => ids.has(r.src) && ids.has(r.dst))
    .map((r, i) => {
      const touches = r.src === centre || r.dst === centre;
      return {
        id: `${r.src}-${r.rel}-${r.dst}-${i}`,
        source: r.src,
        target: r.dst,
        label: touches ? r.rel.toLowerCase().replace(/_/g, " ") : undefined,
        className: touches ? "gedge hot" : "gedge",
        markerEnd: { type: MarkerType.ArrowClosed, width: 14, height: 14 },
        animated: touches,
      };
    });
  return { nodes, edges };
}

export default function AssetPage() {
  const { tenant = "", assetId = "" } = useParams();
  const nav = useNavigate();
  const theme = useEffectiveTheme();
  const id = decodeURIComponent(assetId);
  const { data, error, loading } = usePoll<AssetView>(`/api/v1/estate/${tenant}/assets/${encodeURIComponent(id)}?hops=2`, 15000);

  const graph = useMemo(
    () => (data ? layout(data.graph.assets, data.graph.centre ?? data.asset.id, data.graph.relations) : { nodes: [], edges: [] }),
    [data],
  );

  if (loading) return <div className="page"><Loading label="Loading asset" /></div>;
  if (!data)
    return (
      <div className="page">
        <Link to={`/estate/${tenant}`} className="back">
          <ArrowLeft size={14} /> Estate
        </Link>
        <ErrorNote error={error ?? new Error("Asset not found")} />
      </div>
    );

  const a = data.asset;
  return (
    <div className="page">
      <Link to={`/estate/${tenant}`} className="back">
        <ArrowLeft size={14} /> Estate
      </Link>
      <div className="asset-head">
        <div>
          <div className="row-gap">
            <span className="kind-tag big">{humanize(a.kind)}</span>
            {a.attrs.critical === true && <Tag tone="red">Business critical</Tag>}
          </div>
          <h1>{a.name}</h1>
          <code className="muted">{a.id}</code>
        </div>
        <div className="asset-head-side muted">
          Last seen <Time at={a.seen_at} />
        </div>
      </div>
      <ErrorNote error={error} />

      <div className="asset-grid">
        <Panel
          title="Dependencies"
          icon={<GitFork size={15} />}
          className="graph-panel"
          pad={false}
          actions={<span className="muted small">{data.graph.assets.length} assets · 2 hops · click a node to open it</span>}
        >
          <div className="graph">
            <ReactFlow
              nodes={graph.nodes}
              edges={graph.edges}
              colorMode={theme}
              fitView
              fitViewOptions={{ padding: 0.15 }}
              minZoom={0.2}
              proOptions={{ hideAttribution: true }}
              nodesConnectable={false}
              onNodeClick={(_, n) => n.id !== a.id && nav(`/estate/${tenant}/assets/${encodeURIComponent(n.id)}`)}
            >
              <Background gap={20} size={1} />
              <Controls showInteractive={false} />
            </ReactFlow>
          </div>
        </Panel>

        <div className="asset-side">
          <Panel title="Attributes" icon={<ListTree size={15} />}>
            {Object.keys(a.attrs).length ? (
              <KeyValues rows={Object.entries(a.attrs).map(([k, v]) => [k, <span className="mono">{show(v)}</span>])} />
            ) : (
              <span className="muted">No attributes</span>
            )}
          </Panel>
          <Panel title="Sources" icon={<Radio size={15} />}>
            <div className="tags">
              {a.sources.map((s) => (
                <Tag key={s} tone="blue" mono>
                  {s}
                </Tag>
              ))}
            </div>
          </Panel>
          <Panel title="Related cases" icon={<FolderOpen size={15} />} pad={false}>
            {data.cases.length === 0 ? (
              <Empty title="No cases touched this asset" />
            ) : (
              <ul className="related-cases">
                {data.cases.map((c) => (
                  <li key={c.id}>
                    <Link to={`/cases/${c.id}`}>
                      <SeverityChip severity={c.severity} />
                      <div className="rc-main">
                        <div className="rc-ref">{c.ref}</div>
                        <div className="rc-title">{c.title}</div>
                      </div>
                      <div className="rc-side">
                        <StatusPill status={c.status} />
                        <Time at={c.opened_at} className="muted small" />
                      </div>
                    </Link>
                  </li>
                ))}
              </ul>
            )}
          </Panel>
        </div>
      </div>
    </div>
  );
}
