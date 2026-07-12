"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import type { ReactNode } from "react";

const nav = [
  { href: "/", label: "Runs", mark: "R" },
  { href: "/eval", label: "Evaluations", mark: "E" },
  { href: "/guide", label: "Review guide", mark: "G" },
];

export function AppShell({
  children,
  eyebrow,
  title,
  description,
  actions,
}: {
  children: ReactNode;
  eyebrow: string;
  title: string;
  description?: string;
  actions?: ReactNode;
}) {
  const pathname = usePathname();

  return (
    <div className="app-shell">
      <aside className="app-sidebar">
        <Link href="/" className="brand" aria-label="Portage home">
          <span className="brand-mark">P</span>
          <span>
            <strong>Portage</strong>
            <small>Migration agent</small>
          </span>
        </Link>
        <nav className="app-nav" aria-label="Primary navigation">
          {nav.map((item) => {
            const active =
              item.href === "/" ? pathname === "/" || pathname.startsWith("/jobs/") : pathname.startsWith(item.href);
            return (
              <Link key={item.href} href={item.href} className={active ? "active" : ""}>
                <span className="nav-mark">{item.mark}</span>
                {item.label}
              </Link>
            );
          })}
        </nav>
        <div className="sidebar-foot">
          <span className="live-dot" /> Local control plane
          <small>Diffs stay in your workspace.</small>
        </div>
      </aside>

      <div className="app-workspace">
        <header className="page-header">
          <div>
            <div className="page-eyebrow">{eyebrow}</div>
            <h1>{title}</h1>
            {description && <p>{description}</p>}
          </div>
          {actions && <div className="header-actions">{actions}</div>}
        </header>
        <main className="page-content">{children}</main>
      </div>
    </div>
  );
}
