# AWS Console UI & Cloudscape Design System Guidelines

## Core Design Philosophy
- **Information Density:** High density over decorative whitespace. Use compact density for data-dense views (tables, property lists).
- **Structural Layout:** Always wrap top-level views in Cloudscape `AppLayout` featuring:
  - Collapsible left navigation panel (`SideNavigation`)
  - Main content area with `Header` and breadcrumbs
  - Right-side collapsible drawer (`HelpPanel`) for context help
  - Top `Flashbar` notification area for system/action alerts
- **Predictable Navigation:** Always maintain breadcrumbs (`BreadcrumbGroup`) at the top of main content pages.

## Component Selection & Mapping
- **UI Framework:** Strictly use `@cloudscape-design/components`. Do NOT use generic HTML tags (e.g., `<button>`), Material-UI, or Tailwind.
- **Data Tables:** Use `<Table>` with built-in `<Pagination>`, `<TextFilter>`, and selection controls.
- **Detail Pages:** Use `<Container>` and `<KeyValuePairs>` (or `<ColumnLayout>`) for resource metadata. Use `<Tabs>` for secondary details.
- **Form Controls:** Wrap input groups in `<FormField>` with `label`, `description`, and field-level `constraintText`.
- **Status Badges:** Use `<StatusIndicator>` with types (`success`, `warning`, `error`, `info`, `stopped`) rather than custom color pills.

## Theme & Styling Rules
- Import `@cloudscape-design/global-styles/index.css`.
- Support visual mode toggling using `@cloudscape-design/global-styles/theming` (Light / Dark mode).
- Do not apply custom arbitrary CSS overrides; rely on Cloudscape design tokens for spacing and colors.