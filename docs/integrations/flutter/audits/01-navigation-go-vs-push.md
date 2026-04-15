# Audit 01: `context.go` vs `context.push`

Paste this prompt into a coding assistant running at the root of your
Flutter web project.

---

## Context

An external audit (figma-audit) flagged a systemic navigation problem:
whenever the app navigates to a detail page via `context.push('/route/:id')`,
the widget tree updates and the detail page renders, but `window.location.href`
and `GoRouter.routeInformationProvider.value.uri` do not change. Real
consequences:

- Deep linking to detail pages is broken (share `/invoices/42`, a new
  user lands on `/invoices`)
- Audit tools that track URLs never see the detail page
- URL-based analytics miss every detail navigation
- The browser Back button misbehaves
- Reloading a detail page bounces the user back to the list

Canonical reproducer, for example in an invoices list page:

```dart
return InkWell(
  onTap: () => context.push('/invoices/${invoice.id}'),
  ...
);
```

After this click `InvoiceDetailPage` renders, but
`window.location.pathname` is still `/invoices`.

## Root cause

GoRouter v14+ treats `push` on routes inside a `ShellRoute` as
imperative navigation that does not propagate to the URL bar nor to
the `RouteInformationProvider`. `go` on the other hand is declarative
and updates the URL.

The rule to apply:

- **`context.go(path)`** for every navigation to a URL-addressable
  route, i.e. any route that should be shareable, reloadable or
  reachable from an external link. This covers 99% of user
  navigations.
- **`context.push(path)`** is reserved for actual modals/overlays
  that must not be deep-linkable (e.g. a transient confirmation page
  popped to restore the previous state without ever touching the
  URL).

## Work to perform

### Step 1: exhaustive audit

List every call site of `context.push(` under `lib/`. Classify each
call site into one of three buckets:

- **A. URL-addressable bug**: the target route is a real page with a
  declared path in the app router (e.g. `/invoices/:id`,
  `/profile/:userId`, `/orders/:id`). **Fix: replace
  `context.push(...)` with `context.go(...)`.**
- **B. Legitimate modal/overlay**: the navigation represents a
  transient display with no URL of its own that MUST be poppable
  without changing the URL. **Fix: add a `// GoRouter push
  intentional: <why>` comment explaining the reason.**
- **C. Ambiguous**: you cannot decide. List them and ask before
  touching.

Produce a markdown table with columns: file, line, pushed route,
category, justification.

### Step 2: fix category A

For every A-classified call site, replace `context.push` with
`context.go`. Watch out for these traps:

- `context.push` returns a `Future<T?>` that resolves when the route
  is popped. `context.go` returns `void`. If the call site awaits a
  result (`final result = await context.push(...)`), you need another
  way to retrieve the result (callback, provider, etc.) or keep
  `push` and move the site into category B with the reason "awaits a
  result".
- `context.push(path, extra: obj)` passes an object in memory.
  `context.go` supports `extra` too, so renaming works, BUT passing
  `extra` is fragile on reload (it is lost) so if the detail page
  needs `extra` to render correctly on reload, it must be refactored
  to load its data from the URL id (see step 3).

### Step 3: pages that depend on `extra`

Any page that reads its state from `state.extra` (not only
`state.pathParameters`) is inherently non-reloadable and
non-deep-linkable. These pages need a refactor so they can load their
data from the URL id via a Riverpod (or other state management)
provider, keeping `extra` only as an optimisation to skip the initial
fetch when arriving via internal navigation:

```dart
// Before
class OrderDetailPage extends StatelessWidget {
  final Order order;
  const OrderDetailPage({required this.order});
  // ...
}

// After
class OrderDetailPage extends ConsumerWidget {
  final String orderId;
  final Order? preloadedOrder;  // optional hint from extra
  const OrderDetailPage({required this.orderId, this.preloadedOrder});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final orderAsync = preloadedOrder != null
        ? AsyncValue.data(preloadedOrder!)
        : ref.watch(orderByIdProvider(orderId));
    return orderAsync.when(/* ... */);
  }
}
```

### Step 4: ban the regression

Add a conformance test at `test/lint/navigation_test.dart` that greps
the codebase and fails when a new `context.push(` appears without a
`// GoRouter push intentional:` comment on the same line or the line
above:

```dart
import 'dart:io';
import 'package:test/test.dart';

void main() {
  test('no unexplained context.push', () {
    final libDir = Directory('lib');
    final offenders = <String>[];
    for (final file in libDir.listSync(recursive: true).whereType<File>()) {
      if (!file.path.endsWith('.dart')) continue;
      final lines = file.readAsLinesSync();
      for (var i = 0; i < lines.length; i++) {
        if (lines[i].contains('context.push(')) {
          final thisLine = lines[i];
          final prevLine = i > 0 ? lines[i - 1] : '';
          final hasJustification =
              thisLine.contains('GoRouter push intentional:') ||
              prevLine.contains('GoRouter push intentional:');
          if (!hasJustification) {
            offenders.add('${file.path}:${i + 1}  ${thisLine.trim()}');
          }
        }
      }
    }
    expect(
      offenders,
      isEmpty,
      reason:
          'context.push without justification. Use context.go for '
          'URL-addressable routes, or add "// GoRouter push intentional: '
          '<why>" on the call line or the line above.\n\n'
          '${offenders.join("\n")}',
    );
  });
}
```

### Step 5: document the rule

Add a "Navigation" section to your project conventions file (CLAUDE.md,
README, or equivalent) that explains:

1. The rule: `go` by default, `push` only for transient overlays
2. How to justify a `push` (inline comment)
3. The conformance test that enforces the rule
4. The trap of pages that depend on `extra` (non-reloadable)

## Expected output

1. The markdown audit table (step 1)
2. Diffs for every corrected file (step 2 + step 3)
3. The new conformance test file (step 4)
4. The documentation section you added (step 5)
5. A summary: how many A sites fixed, how many B sites justified, how
   many pages refactored away from `extra`

## Manual verification

1. Run `flutter test test/lint/navigation_test.dart` and confirm the
   check passes.
2. In the browser, go to a list page, click an item. The URL bar
   must switch to the detail route. Copy the URL, open it in a new
   tab. You must land directly on the same detail page.
3. Repeat for every A-classified route you fixed.
