# Audit 02: Stateful URLs for tabs, filters, search and sort

Second volet after audit 01 (`context.push` vs `context.go`). Paste
into a coding assistant running at the root of your Flutter web
project.

---

## Context

The first audit made the app deep-link friendly at the page level:
each detail page now has a unique, shareable, reloadable URL. A whole
class of state is still uncovered though: **the internal state of a
page**, namely active tabs, filters, search queries, sort choices and
pagination. Today this state lives in Riverpod / StatefulWidget and
disappears on reload, is not shareable, and cannot be restored from a
link.

The goal of this second volet is to bring the app to full web
conformance where every user-visible slice of the UI is URL-addressable.
A shared link must reproduce the exact same view, filters and tabs
included.

Example: a `/orders` page displays a list filterable by status, date
range, category. Sharing the URL `/orders` does not share the active
filters. After this audit the link becomes
`/orders?status=pending&category=books&sort=recent` and reproduces the
exact same view on the receiving side.

## Rules

### What MUST live in the URL

- **Visible tabs** (TabBar, SegmentedButton, internal NavigationBar):
  the active tab is an explicit user choice
- **Active filters** (dropdown, checkbox, range slider, chips):
  status, type, period, tags, etc.
- **Text search** (search field): current debounced query
- **Sort** (sort dropdown): column and order
- **Pagination** (page number, offset): only if the app uses actual
  pagination. Infinite scroll has no page number.

### What must NOT live in the URL

- Scroll position
- Draft form state (unsubmitted input)
- Hover, focus, ripple
- Loading spinners, transient errors
- Open context menus, dialogs
- Any ephemeral state a user would never share

### Encoding format

- Simple: `?key=value` for atomic values
- Lists: `?tags=a,b,c` (comma-separated, not URL-encoded JSON)
- Booleans: `?show_archived=true` (explicit, not just `?show_archived`)
- Enums: the `@JsonValue` of the Dart code (e.g. `?status=PENDING`,
  not `?status=0`)
- Omit defaults: if "all statuses" is the default, do not write
  `?status=ALL`, just omit the parameter

### Navigation behaviour

- Changing a tab or filter uses `context.go('/path?new_params')`,
  NOT `context.push`. Rationale: you do not want to stack a history
  entry per chip click.
- The browser Back button must return to the previous PAGE, not the
  previous filter. Therefore `go` which replaces, not `push` which
  stacks.
- Debounce URL updates for text fields (300–500ms) so an update
  does not fire on every keystroke.

## Work to perform

### Step 1: inventory

Walk `lib/features/**/presentation/pages/` and
`lib/features/**/presentation/widgets/` to list every page that
contains at least one of:

- `TabBar`, `TabController`, `DefaultTabController`
- `SegmentedButton`, `CupertinoSegmentedControl`
- `DropdownButton`, `DropdownMenu`, `PopupMenuButton` used as a
  filter chooser (not as an action menu)
- `Checkbox`, `Switch`, `FilterChip` tied to filtering a displayed
  list
- `TextField` / `SearchBar` used as a search field filtering a
  rendered list
- Riverpod provider whose name contains `filter`, `sort`,
  `selectedTab`, `searchQuery`, `pageIndex`, etc.

Produce a markdown table: file, line, state type, description,
default value, candidate URL param name.

### Step 2: classification

For every inventory entry, classify:

- **A. URL-addressable**: the state is an explicit user choice
  (tab, filter, search, sort, page). **Fix: encode into query
  params.**
- **B. Legitimate internal state**: the state is ephemeral or
  technical (loading, hover, focus, form draft, open menu).
  **Fix: leave it alone, add `// stateful URL skip: <why>` above
  the state/provider declaration.**
- **C. Ambiguous**: cannot decide without running the app. List and
  ask.

### Step 3: refactor A pages

Apply the following pattern to every page that has at least one
A-classified state.

#### 3.1 Read query params at build

Use `GoRouterState.of(context).uri.queryParameters` to read. For
enums, parse explicitly with a fallback. For lists, split on comma.

```dart
final uri = GoRouterState.of(context).uri;
final tab = OrdersTab.fromQueryParam(uri.queryParameters['tab']);  // default = current
final statusFilters = (uri.queryParameters['status'] ?? '')
    .split(',')
    .where((s) => s.isNotEmpty)
    .map(OrderStatus.fromJsonValue)
    .whereType<OrderStatus>()
    .toSet();
final search = uri.queryParameters['q'] ?? '';
```

Add a `fromQueryParam(String?)` helper on every enum that returns the
default when input is `null`, empty, or unknown. Be tolerant of
future values.

#### 3.2 Write query params on change

Central helper at `lib/core/router/url_state.dart`:

```dart
/// Update the current URL with new query parameters without losing the
/// current path. Goes through context.go (replace) so filter changes do
/// not flood the browser history.
void updateQueryParams(BuildContext context, Map<String, String?> updates) {
  final router = GoRouter.of(context);
  final current = router.routeInformationProvider.value.uri;
  final next = {...current.queryParameters};
  for (final e in updates.entries) {
    if (e.value == null || e.value!.isEmpty) {
      next.remove(e.key);
    } else {
      next[e.key] = e.value!;
    }
  }
  final newUri = current.replace(queryParameters: next);
  context.go(newUri.toString());
}
```

Usage in a widget that switches a filter:

```dart
SegmentedButton<OrdersTab>(
  selected: {tab},
  onSelectionChanged: (sel) {
    updateQueryParams(context, {'tab': sel.first.queryValue});
  },
  // ...
)
```

For a search field, add debounce:

```dart
Timer? _searchDebounce;
// in onChanged:
_searchDebounce?.cancel();
_searchDebounce = Timer(const Duration(milliseconds: 400), () {
  if (mounted) {
    updateQueryParams(context, {'q': value.isEmpty ? null : value});
  }
});
```

#### 3.3 Riverpod providers driven by URL state

When a provider computes the displayed content (e.g.
`filteredOrdersProvider`), it must read its input from the URL state,
not from another local provider. Two options:

**Option A (recommended)**: the provider takes filters as a `family`
parameter, and the widget passes filters read from the URL at every
`ref.watch`.

```dart
final filteredOrdersProvider = Provider.family<List<Order>, OrderFilters>(
  (ref, filters) {
    final all = ref.watch(ordersProvider).valueOrNull ?? [];
    return all.where((o) => filters.matches(o)).toList();
  },
);

// In the widget:
final filters = OrderFilters.fromUri(GoRouterState.of(context).uri);
final filtered = ref.watch(filteredOrdersProvider(filters));
```

**Option B**: a `urlStateProvider` that exposes the current view of
the URL and notifies on change. Closer to a global store, more
complex to maintain. Avoid unless several widgets far apart in the
tree share the same URL state.

#### 3.4 Remove duplicated internal state

If the page had a `StateProvider` or a `StatefulWidget` that stored
the filter, delete it: the source of truth is now the URL. The widget
rebuilds on URL change via `GoRouterState.of(context)`.

### Step 4: conformance test

Add `test/lint/stateful_urls_test.dart` that detects pages with
`TabController`, `SegmentedButton`, or a provider named
`*Filter*/*SelectedTab*/*SearchQuery*/*Sort*` that does NOT use
`GoRouterState.of(context).uri.queryParameters`. Heuristic, not a
proof, but it catches trivial regressions.

```dart
import 'dart:io';
import 'package:test/test.dart';

void main() {
  test('stateful UI elements read from query params', () {
    final libDir = Directory('lib');
    final suspects = <String>[];
    for (final file in libDir.listSync(recursive: true).whereType<File>()) {
      if (!file.path.endsWith('.dart')) continue;
      if (file.path.contains('/data/') || file.path.contains('/domain/')) continue;
      final text = file.readAsStringSync();
      final hasTabController = text.contains('TabController') ||
          text.contains('SegmentedButton') ||
          text.contains('FilterChip');
      final readsUrl = text.contains('queryParameters') ||
          text.contains('GoRouterState.of(context)') ||
          text.contains('// stateful URL skip:');
      if (hasTabController && !readsUrl) {
        suspects.add(file.path);
      }
    }
    expect(
      suspects,
      isEmpty,
      reason:
          'Pages containing stateful UI (tabs, segmented buttons, filter chips) '
          'must read their state from GoRouterState query params, or add a '
          '"// stateful URL skip: <why>" comment explaining why this state is '
          'legitimately not URL-addressable.\n\n${suspects.join("\n")}',
    );
  });
}
```

### Step 5: documentation

Add a "Stateful URLs" section in your project conventions file:

```markdown
## Stateful URLs

Any user-visible UI slice that represents a user choice (tab, filter,
search, sort, page) must be encoded in the URL query params.

### Rules

- `context.go` for changes (not `push`) to avoid flooding history
- Read state from `GoRouterState.of(context).uri.queryParameters`
- Debounce text fields at 400ms to avoid one update per keystroke
- Omit default values (an "All" filter must not appear in the URL)
- Central helper: `lib/core/router/url_state.dart :: updateQueryParams`
- Enum: add a `fromQueryParam` tolerant of unknown values
- Provider: take filters as a `family` param, pass them from the URL

### Legitimate exclusions

Visual state that is NOT a user choice and must NOT live in the URL:
scroll position, loading spinners, unsubmitted form drafts, hover,
open menus, dialogs. For every exclusion add
`// stateful URL skip: <why>` above the state declaration.

### Conformance test

`flutter test test/lint/stateful_urls_test.dart`
```

## Expected output

1. The inventory markdown table (step 1)
2. A / B / C classification with justification for each entry
3. Diffs of refactored pages (step 3)
4. The `lib/core/router/url_state.dart` helper
5. The conformance test `test/lint/stateful_urls_test.dart`
6. The documentation section added to your conventions file
7. Summary: how many pages refactored, how many states now live in
   URL, how many legitimate exclusions

## Manual verification

1. Go to a list page, apply a filter, copy the URL, open in a new
   tab. The filter must be active immediately.
2. On a page with tabs, select a non-default tab, reload. The tab
   must be preserved.
3. Change a filter, press Back. The URL must return to the previous
   browser URL (previous page), not the previous filter state.
4. In a search field, type "hello", wait 500ms, check the URL: it
   must contain `?q=hello`. Clear the field: `?q=hello` must
   disappear.
