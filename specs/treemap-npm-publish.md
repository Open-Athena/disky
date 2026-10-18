# Publish `@rdub/treemap` to npm (before Mon 10/19)

From the disky deep-dive deck (`~/c/oa/disky-deep-dive`), 2026-10-18. Ryan OK'd publishing ("tell US to do it"). The talk's treemap slide demos the package live, and would rather say `pnpm add @rdub/treemap` than "pin a dist-branch SHA".

## State

- `packages/treemap` is `0.1.0`. `main` / `types` / `exports` point at `./src/*.ts(x)`, and `build` is only `tsc --noEmit`.
- It ships on the npm-dist `dist/treemap` branch, which is the same raw TS/TSX source. The newest dist is `ed136b03` (from `5b6a9b7`, 10/14).
- `build-dist.yml` triggers on pushes to `main`, but the working branch is `cloud`. Is that why the last dist is from 10/14? Check.
- **Consumers have to compile the TSX themselves.** The deck hit this. Vite's dep pre-bundling (rolldown) left JSX raw in `.vite/deps/@rdub_treemap.js`, and the default oxc pass did the same for the raw files. The deck needed a custom `transformWithOxc` pre plugin plus `optimizeDeps.exclude` (see the deck's `vite.config.ts`). A published package shouldn't need that.
- npm: `npm whoami` is `rdub` (owner of the `@rdub` scope), and `@rdub/treemap` is unclaimed.

## Ask

1. **Compiled build** with tsup, matching `~/c/js/use-kbd/tsup.config.ts`:
   - entries: `src/index.ts`, `squarify`, `parseColor`, `divergingColor`, `svg`, `voronoi/index`;
   - ESM (CJS optional), `dts: true`, sourcemaps;
   - `external: ['react', 'react-dom', 'd3-hierarchy', 'd3-voronoi-treemap']`;
   - JSX runtime automatic;
   - `styles.css` copied to `dist/`.
2. **Keep the workspace on source.** `site/` and `packages/react` use `workspace:*` and benefit from importing `src` directly (HMR, no build step). So leave `main` / `exports` on `src`, and put the dist entry points in `publishConfig` (`main`, `module`, `types`, `exports` per subpath, including `./styles.css`). pnpm rewrites those at `pnpm publish` / `pack` time. Set `files: ["dist", "README.md"]` for the published tarball.
3. **Check before publishing:**
   - `pnpm -C packages/treemap pack`, then confirm the tarball's `package.json` points at `dist/` and contains no `.tsx`.
   - Install the tarball into a scratch Vite + React app under `tmp/` and render a `<Treemap>`, with **no** custom JSX config. That's the case that broke.
   - `pnpm -C packages/treemap test`.
4. **Publish** `0.1.0` with `pnpm publish --access public` from `packages/treemap`, then tag `treemap-v0.1.0`.
5. **README:** replace the "no npm publish (yet)" install section with `pnpm add @rdub/treemap`, keeping the dist-branch SHA pin as an alternative. Keep the `import '@rdub/treemap/styles.css'` note.
6. **Later, optional:** a `publish-treemap.yml` (tag-triggered, `NPM_TOKEN` secret), so later releases aren't laptop-only. Not needed for Monday.

## Consumer follow-up (the deck, not this repo)

Once it's on npm, the deck switches to `@rdub/treemap@^0.1.0` and drops its `rdub-treemap-tsx` Vite plugin and `optimizeDeps` tweaks. Tell Ryan the version; the deck session does the swap.
