# Efferd Dashboard 1 (local reconstruction)

The `@efferd/dashboard-1` shadcn block — a welcome header with KPI stats, a sales
area chart, and a recent-invoices table in a bordered three-column grid.

## Why this is hand-built rather than `shadcn add`

This environment's network egress policy blocks both `efferd.com` and
`ui.shadcn.com`, so `npx shadcn@latest add @efferd/dashboard-1` cannot fetch any
registry item. Instead the block was reconstructed from the registry manifest
(`dashboard-1.json`) plus npm packages, which are reachable.

Everything the manifest shipped is reproduced verbatim:

| File | Source |
| --- | --- |
| `src/components/dashboard.tsx` | manifest, verbatim |
| `src/components/dashboard-invoices.tsx` | manifest, verbatim |
| `src/components/sales-chart.tsx` | manifest, verbatim |
| `src/components/stats.tsx` | manifest, verbatim |
| `src/components/delta.tsx` | manifest, verbatim + one added `IconPlaceholder` import |

### Local stand-ins for blocked dependencies

These could not be fetched from efferd.com and were reconstructed locally. Swap
them for the upstream versions once the registry is reachable:

- `src/lib/formater.ts` — stands in for `@efferd/formater` (`formatDate`).
- `src/components/app-shell.tsx` — stands in for `@efferd/app-shell-1`.
- `src/components/icon-placeholder.tsx` — the `IconPlaceholder` that `delta.tsx`
  references but the manifest never ships (it resolves lucide icons).

The shadcn primitives (`badge`, `card`, `chart`, `select`, `table`) are the
standard shadcn/ui "new-york" implementations, written locally because
`ui.shadcn.com` is also blocked.

## Run

```bash
cd dashboard
npm install
npm run dev       # start the dev server
npm run build     # typecheck + production build
```

## Restoring the true upstream block

When `efferd.com` and `ui.shadcn.com` are reachable, `components.json` already
carries the `@efferd` registry, so you can re-pull the canonical versions:

```bash
npx shadcn@latest add @efferd/formater
npx shadcn@latest add @efferd/app-shell-1
npx shadcn@latest add @efferd/dashboard-1
```
