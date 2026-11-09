// Small line icons for the explorer nav (24x24, stroke = currentColor).
const PATHS: Record<string, string> = {
  chat: "M4 19l1.6-4.2A7.5 7.5 0 1 1 8.4 18z",
  search: "M10.5 17a6.5 6.5 0 1 0 0-13 6.5 6.5 0 0 0 0 13zM20 20l-4.6-4.6",
  graph: "M6 7a2 2 0 1 0 0-.01M18 7a2 2 0 1 0 0-.01M12 18a2 2 0 1 0 0-.01M7.6 8.2l3.2 7.8M16.4 8.2l-3.2 7.8M8 7h8",
  agent: "M6 9h12v10H6zM12 4v5M9.5 13h.01M14.5 13h.01M3 14h3M18 14h3",
  store: "M4 9l2-5h12l2 5M4 9v11h16V9M4 9h16M10 20v-6h4v6",
  space: "M4 4h6v6H4zM14 4h6v6h-6zM4 14h6v6H4zM14 14h6v6h-6z",
  db: "M5 6c0-1.4 3.1-2.5 7-2.5s7 1.1 7 2.5-3.1 2.5-7 2.5S5 7.4 5 6zM5 6v12c0 1.4 3.1 2.5 7 2.5s7-1.1 7-2.5V6M5 12c0 1.4 3.1 2.5 7 2.5s7-1.1 7-2.5",
  book: "M6 4h9l3 3v13H6zM15 4v3h3M9 11h6M9 15h4",
  onto: "M12 3l8 4.5v9L12 21l-8-4.5v-9zM12 12l8-4.5M12 12v9M12 12L4 7.5",
  plug: "M9 3v5M15 3v5M6 8h12v3a6 6 0 0 1-12 0zM12 17v4",
  shield: "M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6zM9 12l2 2 4-4",
  pulse: "M3 12h4l2-6 4 12 2-6h6",
  panel: "M4 5h16v14H4zM9 5v14",
  chev: "M9 6l6 6-6 6",
};

export function Icon({ name, className = "i" }: { name: string; className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" aria-hidden="true">
      <path d={PATHS[name] ?? PATHS.space} />
    </svg>
  );
}
