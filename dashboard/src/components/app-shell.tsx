import type * as React from "react";

/**
 * Minimal stand-in for the `@efferd/app-shell-1` registry dependency, which
 * lives on efferd.com and could not be fetched in this environment. The real
 * app-shell provides a sidebar + header chrome; this wrapper just supplies the
 * padded, centered content column the dashboard block expects to render into.
 *
 * To install the real shell once efferd.com is reachable:
 *   npx shadcn@latest add @efferd/app-shell-1
 */
export function AppShell({ children }: { children: React.ReactNode }) {
	return (
		<div className="min-h-svh bg-background text-foreground">
			<header className="border-b">
				<div className="mx-auto flex h-14 w-full max-w-6xl items-center gap-2 px-4 sm:px-6">
					<div className="size-6 rounded-md bg-primary" aria-hidden />
					<span className="font-semibold">Efferd</span>
				</div>
			</header>
			<main className="mx-auto w-full max-w-6xl px-4 sm:px-6">{children}</main>
		</div>
	);
}
