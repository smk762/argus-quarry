"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

const NAV = [
  { href: "/", label: "Run", match: (p: string) => p === "/" },
  { href: "/records/", label: "Candidates", match: (p: string) => p.startsWith("/records") || p.startsWith("/record/") },
];

export function NavLinks() {
  const pathname = usePathname();
  return (
    <nav aria-label="Pages" className="flex items-center gap-1">
      {NAV.map((item) => {
        const on = item.match(pathname);
        return (
          <Link
            key={item.href}
            href={item.href}
            aria-current={on ? "page" : undefined}
            className={`rounded-md px-2.5 py-1.5 text-small ${on ? "bg-base-700 text-text-primary" : "text-text-secondary hover:bg-base-700 hover:text-text-primary"}`}
          >
            {item.label}
          </Link>
        );
      })}
      <a href="https://github.com/smk762/argus-quarry" className="rounded-md px-2.5 py-1.5 text-small text-text-secondary hover:bg-base-700 hover:text-text-primary">
        GitHub
      </a>
    </nav>
  );
}
