# Audit 04: Semantics on custom tappable widgets

Fourth volet after audits 01 (navigation), 02 (stateful URLs) and 03
(wizards). Paste into a coding assistant running at the root of your
Flutter web project.

---

## Context

An external audit (figma-audit) flagged interactive widgets that are
invisible to accessibility tools and test automation. The typical
case: a `GestureDetector` or `InkWell` wrapping a custom `Container`,
with no explicit `Semantics`. Flutter Web then emits no
`<flt-semantics role="button">` node for the widget, and:

- Screen readers do not detect it (real accessibility impact, WCAG
  2.1 1.3.1 "Info and Relationships" and 4.1.2 "Name, Role, Value"
  violations)
- Playwright cannot target it via `getByRole('button', ...)`
- e2e tests fall back to fragile coordinate clicks
- figma-audit cannot reliably execute click-through reach_paths

A concrete pattern: the "Next" button of a wizard that does not
respond to automated click attempts even though it responds perfectly
to a human click. The most likely cause: an `InkWell(onTap: ...)`
wrapping a `Container(child: Text('Next'))` with no Semantics
annotation.

## Rule

Every logically interactive widget (reacts to tap/click and triggers
an action) must emit a usable Semantics node. Three acceptable
approaches:

**A. Use a standard Button widget** (preferred for new code):
`ElevatedButton`, `FilledButton`, `TextButton`, `OutlinedButton`,
`IconButton`, `FloatingActionButton`. These widgets automatically
emit a `Semantics(button: true, label: <text>)` based on their child.

**B. Wrap in an explicit `Semantics`** (for custom widgets that
cannot be replaced by a standard button):

```dart
Semantics(
  button: true,
  label: 'Claim order',
  enabled: _canClaim,
  child: InkWell(
    onTap: _canClaim ? _claimOrder : null,
    child: _buildCustomButtonContent(),
  ),
)
```

**C. Use `MergeSemantics` on a group** when the widget is composed
of multiple sub-widgets that must appear as a single button to
screen readers:

```dart
MergeSemantics(
  child: GestureDetector(
    onTap: _onTap,
    child: Column(children: [Icon(...), Text('Confirm')]),
  ),
)
```

A `GestureDetector` or `InkWell` with a non-null `onTap` and no
Semantics is an accessibility bug.

## Legitimate exclusions

- Tap zones triggering a purely visual animation with no real action
  (ripple effect on an expanding card)
- Tap zones whose semantics are carried by an enclosing parent
  `Semantics` (use `excludeSemantics: true` on the child to avoid
  duplication)
- Tappable dev/debug widgets not shipped in production

For every exclusion add `// a11y skip: <why>` above the `onTap`.

## Work to perform

### Step 1: inventory

Grep every tappable widget under `lib/features/` and
`lib/shared/widgets/` that does not emit Semantics:

```bash
grep -rn "GestureDetector(\|InkWell(\|InkResponse(" lib/features/ lib/shared/widgets/
```

For every occurrence, inspect the enclosing widget and determine:

1. Is there a non-null `onTap`? If not, it is probably just a ripple
   or hit test, skip.
2. Is the widget wrapped in `Semantics(button: true, ...)` or
   `MergeSemantics`? If yes, OK.
3. Is the direct parent a standard Button widget
   (`ElevatedButton`/`FilledButton`/etc.)? If yes, OK.
4. Otherwise, it is a migration candidate.

Produce a table: file, line, tappable widget type
(`GestureDetector`/`InkWell`/`InkResponse`), visible text or
`onTap` action, direct parent widget, classification.

### Step 2: classification

For every candidate:

- **A. Migrate to a standard Button**: the tappable widget looks
  like a classic button (text + optional icon, solid background,
  simple action). Cleanest option.
- **B. Wrap in Semantics**: the widget has a custom design that
  cannot be reproduced with a standard button (interactive card,
  custom tile, pictogram + badge + label stack). Keep the widget,
  add the Semantics parent.
- **C. MergeSemantics**: multiple sub-widgets form one logical
  element (Icon + Text in a tappable Column).
- **D. Legitimate exclusion**: add `// a11y skip: <why>`.

### Step 3: migration

#### 3.1 Case A — standard Button

```dart
// Before
InkWell(
  onTap: _onSignUp,
  borderRadius: BorderRadius.circular(8),
  child: Container(
    padding: const EdgeInsets.symmetric(horizontal: 24, vertical: 12),
    decoration: BoxDecoration(
      color: Colors.blue,
      borderRadius: BorderRadius.circular(8),
    ),
    child: const Text('Sign up'),
  ),
)

// After
FilledButton(
  onPressed: _onSignUp,
  child: const Text('Sign up'),
)
```

#### 3.2 Case B — Semantics wrapper

```dart
// Before
InkWell(
  onTap: () => context.go('/profile/$userId'),
  child: _buildUserCard(user),
)

// After
Semantics(
  button: true,
  label: 'Open profile of ${user.displayName}',
  child: InkWell(
    onTap: () => context.go('/profile/$userId'),
    child: ExcludeSemantics(child: _buildUserCard(user)),
  ),
)
```

Note: `ExcludeSemantics` on the child prevents the inner texts of
the card (name, role, etc.) from being read separately. The parent
Semantics label is enough.

#### 3.3 Case C — MergeSemantics

```dart
// Before
GestureDetector(
  onTap: _toggleDarkMode,
  child: Column(
    children: [
      Icon(Icons.dark_mode),
      Text('Dark mode'),
    ],
  ),
)

// After
MergeSemantics(
  child: GestureDetector(
    onTap: _toggleDarkMode,
    child: Column(
      children: [
        Icon(Icons.dark_mode),
        Text('Dark mode'),
      ],
    ),
  ),
)
```

Flutter merges the Icon and Text Semantics into a single node, and
the `onTap` of the GestureDetector makes it button-like.

#### 3.4 Case D — exclusion

```dart
// a11y skip: ripple animation on long-press that shows a context
// menu. The real actions live on the menu items which have their
// own Semantics.
GestureDetector(
  onLongPress: _showContextMenu,
  child: _buildCard(),
)
```

### Step 4: conformance test

Create `test/lint/a11y_semantics_test.dart`:

```dart
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';

/// Conformance lint: every GestureDetector/InkWell with a non-null
/// onTap must be accompanied by a Semantics(button: true, ...)
/// above, or a MergeSemantics, or a standard Button parent widget,
/// or an `// a11y skip:` comment justifying the exception.
void main() {
  test('no unexplained tappable widget without Semantics', () {
    final libDir = Directory('lib');
    final offenders = <String>[];
    final tappablePattern = RegExp(
      r'(GestureDetector|InkWell|InkResponse)\s*\(',
    );
    for (final file in libDir.listSync(recursive: true).whereType<File>()) {
      if (!file.path.endsWith('.dart')) continue;
      final lines = file.readAsLinesSync();
      for (var i = 0; i < lines.length; i++) {
        if (!tappablePattern.hasMatch(lines[i])) continue;
        // Is onTap null? Look ahead ~15 lines for `onTap: null` or
        // no `onTap:` at all.
        final chunk = lines.skip(i).take(15).join('\n');
        if (!chunk.contains('onTap:')) continue;
        if (RegExp(r'onTap:\s*null').hasMatch(chunk)) continue;
        // Look 8 lines back for Semantics, MergeSemantics, a Button
        // parent, or the a11y skip marker.
        final prev = i >= 8
            ? lines.sublist(i - 8, i).join('\n')
            : lines.sublist(0, i).join('\n');
        final ok = prev.contains('Semantics(') ||
            prev.contains('MergeSemantics(') ||
            prev.contains('// a11y skip:') ||
            RegExp(
              r'(ElevatedButton|FilledButton|TextButton|OutlinedButton|IconButton|FloatingActionButton)\s*\(',
            ).hasMatch(prev);
        if (!ok) {
          offenders.add('${file.path}:${i + 1}  ${lines[i].trim()}');
        }
      }
    }
    expect(
      offenders,
      isEmpty,
      reason:
          'Tappable widgets (GestureDetector/InkWell/InkResponse with '
          'onTap non-null) must be wrapped in Semantics(button: true, '
          'label: ...) or MergeSemantics, or use a standard Button widget, '
          'or carry a "// a11y skip: <why>" comment.\n\n${offenders.join("\n")}',
    );
  });
}
```

Note: heuristic, not a formal lint. It catches the obvious cases.
Some false positives are possible (onTap defined later, Semantics in
a far-away wrapper); adjust the test as needed or accept a temporary
`// a11y skip:` comment to silence them.

### Step 5: documentation

Add an "Accessibility of interactive widgets" section to your
conventions file:

```markdown
## Accessibility of interactive widgets

Every logically interactive widget (triggers an action on tap/click)
must emit a Semantics node usable by screen readers, e2e tests and
automated audit tools.

### Rules

- Prefer standard Button widgets (`FilledButton`, `ElevatedButton`,
  `TextButton`, `IconButton`, etc.) which emit Semantics
  automatically
- For custom widgets (InkWell/GestureDetector on a custom layout),
  wrap in `Semantics(button: true, label: ..., enabled: ...)`
- For groups (Icon + Text + Badge forming one logical button), use
  `MergeSemantics` around the parent widget
- Inner texts that must not be read separately can be wrapped in
  `ExcludeSemantics`

### When to exclude

Tappable elements triggering a purely visual effect with no real
action (ripple, long-press showing a context menu whose items have
their own Semantics, etc.) can be excluded. For every exclusion add
`// a11y skip: <why>` above the `onTap`.

### Conformance test

`flutter test test/lint/a11y_semantics_test.dart` runs in CI.
```

## Expected output

1. The inventory markdown table
2. A / B / C / D classification with justification
3. Diffs of migrated widgets
4. The conformance test
5. The documentation section added
6. Summary: how many widgets migrated to Button, how many wrapped
   in Semantics, how many in MergeSemantics, how many exclusions

## Manual verification

1. Open a screen reader on the staging app (VoiceOver on iOS,
   TalkBack on Android, NVDA on Windows). Walk a migrated page, the
   reader must announce each button ("Sign up, button").
2. In the browser DevTools on staging:
   `document.querySelectorAll('flt-semantics[role="button"]').length`
   must return a higher number than before (proportional to the
   number of migrated widgets).
3. A minimal Playwright script:
   ```js
   const btn = await page.getByRole('button', { name: 'Next' }).first();
   await btn.click();
   ```
   must work on every migrated wizard.
