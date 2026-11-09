// Explorer navigation (Phase 8). The menu mirrors the "Ontology Explorer" concept: nine groups. Every item is
// either wired to a real gateway-backed page (`to`) or honestly marked as not built (`notBuilt`). Visibility
// here is cosmetic: the gateway refuses anything the caller may not do, whatever the menu shows.
import type { Me } from "./api";

export type Need = "maker" | "checker" | "governance" | "ops" | "reader";

export interface NavItem {
  label: string;
  to?: string;
  notBuilt?: string; // what it would take (shown on the placeholder page)
  need?: Need;
}

export interface NavGroup {
  title: string;
  icon: string;
  items: NavItem[];
}

export const TOP: NavItem[] = [
  { label: "New chat", to: "/chat" },
  { label: "Search", to: "/search" },
  { label: "Explore graph", to: "/ontology" },
];

export const GROUPS: NavGroup[] = [
  {
    title: "Enterprise Agent Space",
    icon: "agent",
    items: [
      { label: "AI Chat Agent", to: "/chat" },
      { label: "Research Agent", notBuilt: "a multi-step agent that plans several gateway calls (documents, data, graph) under the caller's identity, with every step audited" },
      { label: "AI Dashboard & Analytics", notBuilt: "user-built dashboards over the marts; needs saved queries executed through the SQL policy engine as the viewer" },
      { label: "Agent App/Service", notBuilt: "packaging an agent as a service; needs service identities and a gateway client for them" },
    ],
  },
  {
    title: "Enterprise Marketplace",
    icon: "store",
    items: [
      { label: "Data Products", to: "/catalog/marts" },
      { label: "Knowledge Products", to: "/spaces" },
      { label: "Semantic Products", notBuilt: "published semantic models and metrics; depends on Semantic Model & Metrics" },
      { label: "Agent Tools & APIs", notBuilt: "a catalogue of tools agents may call; depends on the MCP tool registry" },
    ],
  },
  {
    title: "Knowledge Space Management",
    icon: "space",
    items: [
      { label: "Register / Manage Space", to: "/portal", need: "maker" },
      { label: "Access & Ownership", to: "/access" },
      { label: "Space Asset Inventory", to: "/spaces" },
    ],
  },
  {
    title: "Data Management",
    icon: "db",
    items: [
      { label: "Data Source Registration", to: "/portal", need: "maker" },
      { label: "Data Product Registration", notBuilt: "registering a mart table as a product (owner, grants, masks) through maker-checker; today marts and policies are config files" },
      { label: "Data Ingestion & Sync", to: "/ops", need: "ops" },
      { label: "Data Approval & Lifecycle", to: "/inbox", need: "checker" },
    ],
  },
  {
    title: "Knowledge Management",
    icon: "book",
    items: [
      { label: "Knowledge Registration", to: "/portal", need: "maker" },
      { label: "Knowledge Upload & Ingestion", to: "/uploads" },
      { label: "Knowledge Processing & Indexing", to: "/flags", need: "governance" },
      { label: "Knowledge Approval & Lifecycle", to: "/inbox", need: "checker" },
    ],
  },
  {
    title: "Ontology & Semantic Management",
    icon: "onto",
    items: [
      { label: "Ontology Management", to: "/ontology/browse" },
      { label: "Entity & Relationship Mapping", to: "/ontology/mapping" },
      { label: "Knowledge Graph Management", to: "/ontology/graphs", need: "reader" },
      { label: "Semantic Model & Metrics", notBuilt: "metric definitions bound to ontology concepts and compiled to SQL per user; needs a semantic layer and its own four-eyes flow" },
    ],
  },
  {
    title: "Agent Integration & Tools",
    icon: "plug",
    items: [
      { label: "MCP Server & Tool Registry", notBuilt: "registering MCP servers; every tool call would still have to pass through the gateway (one door) as the end user" },
      { label: "Agent Access & Permissions", notBuilt: "per-agent scopes on top of the user's own groups (never wider)" },
      { label: "Tool Testing & Publishing", notBuilt: "a sandbox and maker-checker publication for tools" },
    ],
  },
  {
    title: "Data/Knowledge Governance",
    icon: "shield",
    items: [
      { label: "Governance & Security", to: "/governance/approvals", need: "governance" },
      { label: "Access Policies & Classification", to: "/access" },
      { label: "Approval Workflows", to: "/inbox", need: "checker" },
      { label: "Audit & Lineage", to: "/activity" },
    ],
  },
  {
    title: "Quality & Observability",
    icon: "pulse",
    items: [
      { label: "Data & Knowledge Quality", to: "/quality" },
      { label: "Retrieval / RAG Evaluation", to: "/ops#quality", need: "ops" },
      { label: "Pipeline & Agent Monitoring", to: "/ops", need: "ops" },
    ],
  },
];

export function allowed(item: NavItem, me: Me): boolean {
  switch (item.need) {
    case "maker":
      return me.maker_bus.length > 0;
    case "checker":
      return me.checker_bus.length > 0;
    case "governance":
      return me.maker_bus.length > 0 || me.checker_bus.length > 0;
    case "ops":
      return me.ops;
    case "reader":
      return me.spaces.length > 0;
    default:
      return true;
  }
}

export function slug(label: string): string {
  return label.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/(^-|-$)/g, "");
}

export function findNotBuilt(s: string): { group: string; item: NavItem } | null {
  for (const g of GROUPS) for (const i of g.items) if (i.notBuilt && slug(i.label) === s) return { group: g.title, item: i };
  return null;
}

export function crumbFor(path: string): { group: string; label: string } | null {
  const top = TOP.find((t) => t.to === path);
  if (top) return { group: "Explorer", label: top.label };
  let hit: { group: string; label: string } | null = null; // last match: the most specific group (e.g. /ops)
  for (const g of GROUPS) for (const i of g.items) if (i.to && i.to.split("#")[0] === path) hit = { group: g.title, label: i.label };
  return hit;
}
