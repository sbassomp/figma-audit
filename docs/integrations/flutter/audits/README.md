# Flutter audit prompts

This folder contains four drop-in prompts you can paste into a coding
assistant (Claude Code, Cursor, Continue, etc.) to bring a Flutter web
app up to a state where figma-audit can reliably capture it. Each
prompt is fully self-contained: it explains what the problem is, why
it matters, the rule to apply, the work to perform, and a short
verification checklist.

Running these four audits in order on a mature Flutter web codebase
typically takes a coding assistant a few hours and produces a diff
that eliminates the most common reasons figma-audit mis-navigates or
fails to click widgets.

| # | Audit | What it fixes |
|---|---|---|
| [01](./01-navigation-go-vs-push.md) | `context.go` vs `context.push` | Detail pages whose URL does not update when navigated to, breaking deep links and audit tools |
| [02](./02-stateful-urls.md) | Stateful URLs for tabs and filters | Tabs, filters, search and sort that live in widget state instead of query params, so a shared link never reproduces what the user sees |
| [03](./03-wizard-urls.md) | Wizard steps in URL | Multi-step forms whose current step is a private `int _currentStep`, so reloading the page loses progress and figma-audit cannot capture each step |
| [04](./04-semantics.md) | Semantics on custom tappable widgets | `GestureDetector`/`InkWell` buttons that emit no accessibility node, so screen readers and test automation cannot find them |

## How to use

1. Open the coding assistant at the root of your Flutter web project.
2. Start with [01-navigation-go-vs-push.md](./01-navigation-go-vs-push.md).
   Paste the full content, review the audit table the assistant
   produces, then merge its diff.
3. Move on to [02-stateful-urls.md](./02-stateful-urls.md), then
   [03-wizard-urls.md](./03-wizard-urls.md), then
   [04-semantics.md](./04-semantics.md). They are ordered from most to
   least impactful on figma-audit reliability.
4. After each audit, run the conformance test the assistant added to
   `test/lint/` so you catch regressions in CI.

Each audit is independent: you can run them in isolation if some
already apply to your codebase. The ordering above is the recommended
path when all four are needed.

## Scope

These audits target Flutter web apps using **GoRouter**. Apps built on
Navigator 1.0 need different fixes (not covered here). Apps using
other routers (`auto_route`, `beamer`) should translate the GoRouter
snippets to their router of choice. The principles transfer verbatim.

The audits are safe to re-run: each one emits a conformance test that
fails on regressions. You can wire these tests into CI so future PRs
cannot reintroduce the patterns the audit eliminated.
