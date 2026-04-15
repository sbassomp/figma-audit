# Audit 03: Wizard steps in URL

Third volet after audits 01 (`context.go` vs `context.push`) and 02
(stateful URLs for tabs and filters). Paste into a coding assistant
running at the root of your Flutter web project.

---

## Context

The previous audits made the app fully URL-addressable for detail
pages, tabs and filters. One class of state is still uncovered:
**the current step of multi-step wizards** (create-order flows,
registration, checkout, document completion, etc.). These wizards
typically store the current step in a `int _currentStep = 0` of a
`StatefulWidget` and advance via `setState(() => _currentStep++)`,
without touching the URL.

Consequences:

- A user who reloads mid-wizard goes back to step 1 and loses
  progress
- The browser Back button cannot return to the previous step (it
  exits the wizard entirely)
- Shared links cannot point to a specific step
- Automation tools cannot capture each step individually without
  fragile clicks on the "Next" button (which may be disabled by
  form validation)
- URL-based analytics cannot tell "wizard abandoned at step 1" from
  "wizard abandoned at step 3"

Typical examples to audit in a Flutter web app:

- Multi-step creation flows (4+ screens for a new entity)
- Registration with phone/email verification, profile, consent
- Checkout with shipping, payment, confirmation
- Any `PageView`/`Stepper`/`IndexedStack` driven by a
  `_currentStep` int inside a StatefulWidget

## Rule

Every wizard step must be encoded in the container page URL via a
`?step=N` or (preferred) `?step=<name>` query param. The rule is
identical to tabs and filters:

- **`context.go`** to change step (not `push`) to avoid flooding
  history
- **Read the step** from
  `GoRouterState.of(context).uri.queryParameters['step']`
- **Write the step** via `updateQueryParams(context, {'step': next})`
- **Omit the default**: step 1 must not appear in the URL
- **Name steps with a stable identifier**: prefer `?step=addresses`
  over `?step=1` so the order can change without breaking shared
  links

Form controllers (`TextEditingController`, draft objects) keep their
local state — they are user drafts, not URL-addressable. Comment
them with `// stateful URL skip: draft user input`.

## Work to perform

### Step 1: inventory

Walk `lib/features/**/presentation/pages/` looking for wizards.
Indicators to grep:

```bash
grep -rln "int _currentStep\|int _step\|_stepIndex\|_pageIndex\|PageView\|Stepper\|IndexedStack" lib/features/
```

For every page that contains a wizard, produce a table row: file,
number of steps, current step names (if readable from code),
advancement mechanism
(`setState`/`PageController.animateToPage`/etc.).

### Step 2: classification

For every wizard:

- **A. Migrate**: wizard that navigates linearly between distinct
  steps, each showing significantly different content the user
  might want to resume. Examples: entity creation, multi-page
  registration, document completion.
- **B. Leave it**: purely technical internal state that should not
  be shared (intermediate calculation step, transition animation,
  inline delete confirmation). Add `// stateful URL skip: <why>` on
  the state declaration.
- **C. Ambiguous**: ask before touching.

### Step 3: refactor A wizards

Apply the following pattern to each.

#### 3.1 Declare a typed enum for steps

```dart
enum CreateOrderStep {
  category('category'),
  addresses('addresses'),
  schedule('schedule'),
  confirm('confirm');

  final String queryValue;
  const CreateOrderStep(this.queryValue);

  static CreateOrderStep fromQueryParam(String? v) {
    for (final step in CreateOrderStep.values) {
      if (step.queryValue == v) return step;
    }
    return CreateOrderStep.values.first;
  }

  CreateOrderStep? get next {
    final i = index;
    if (i + 1 >= CreateOrderStep.values.length) return null;
    return CreateOrderStep.values[i + 1];
  }

  CreateOrderStep? get previous {
    if (index == 0) return null;
    return CreateOrderStep.values[index - 1];
  }
}
```

#### 3.2 Read step from URL

```dart
@override
Widget build(BuildContext context) {
  final step = CreateOrderStep.fromQueryParam(
    GoRouterState.of(context).uri.queryParameters['step'],
  );
  return Scaffold(
    appBar: AppBar(title: Text(_titleFor(step))),
    body: _bodyFor(step),
    bottomNavigationBar: _navButtons(context, step),
  );
}
```

#### 3.3 Navigate between steps via URL

```dart
Widget _navButtons(BuildContext context, CreateOrderStep step) {
  return Row(
    children: [
      if (step.previous != null)
        TextButton(
          onPressed: () => updateQueryParams(context, {
            'step': step.previous == CreateOrderStep.values.first
                ? null
                : step.previous!.queryValue,
          }),
          child: const Text('Back'),
        ),
      const Spacer(),
      if (step.next != null)
        FilledButton(
          onPressed: _canAdvanceFrom(step)
              ? () => updateQueryParams(context, {
                    'step': step.next!.queryValue,
                  })
              : null,
          child: const Text('Next'),
        )
      else
        FilledButton(
          onPressed: _canSubmit() ? _submit : null,
          child: const Text('Create'),
        ),
    ],
  );
}
```

#### 3.4 Keep field state local

`TextEditingController`, in-progress draft objects, and any form
state stay in the widget `State` (or a dedicated wizard provider).
These DO NOT go in the URL. Comment:

```dart
// stateful URL skip: draft input for the create-order wizard,
// non-shareable and non-reloadable by design.
final _addressController = TextEditingController();
```

#### 3.5 Per-step validation

The "Next" button stays disabled until the required fields of the
current step are valid (`_canAdvanceFrom(step)`). That is business
logic and does not change. Side benefit: automation can skip the
validation by navigating directly to `?step=confirm` without
filling the earlier steps. The "Create" button will still be
disabled if the state is invalid, which is the intended behaviour.

### Step 4: Semantics on nav buttons

Take advantage of the refactor to check that the "Next" and "Back"
buttons are accessible via Semantics. If you use
`FilledButton`/`TextButton`/`ElevatedButton`, they are by default.
If you have a custom widget wrapped in a `GestureDetector` or
`InkWell`, add:

```dart
Semantics(
  button: true,
  label: 'Next',
  enabled: _canAdvanceFrom(step),
  child: InkWell(onTap: ..., child: ...),
)
```

(Audit 04 covers this in full.)

### Step 5: conformance test

Extend `test/lint/stateful_urls_test.dart` or create
`test/lint/wizard_urls_test.dart` that detects suspect patterns:

```dart
import 'dart:io';
import 'package:flutter_test/flutter_test.dart';

void main() {
  test('no unexplained _currentStep in pages', () {
    final libDir = Directory('lib/features');
    final offenders = <String>[];
    final stepPattern = RegExp(r'int\s+_current(Step|PageIndex|StepIndex)');
    for (final file in libDir.listSync(recursive: true).whereType<File>()) {
      if (!file.path.endsWith('.dart')) continue;
      final text = file.readAsStringSync();
      if (!stepPattern.hasMatch(text)) continue;
      final readsUrl = text.contains('queryParameters') ||
          text.contains('// stateful URL skip:');
      if (!readsUrl) {
        offenders.add(file.path);
      }
    }
    expect(
      offenders,
      isEmpty,
      reason:
          'Wizard-style pages (int _currentStep/_pageIndex/etc.) must read '
          'their step from GoRouterState query params, or add a '
          '"// stateful URL skip: <why>" comment explaining why this step '
          'is legitimately not URL-addressable.\n\n${offenders.join("\n")}',
    );
  });
}
```

### Step 6: documentation

Add a "Wizards" sub-section under "Stateful URLs" in your
conventions file:

```markdown
### Multi-step wizards

Wizards (entity creation, multi-page registration, checkout, document
completion) must encode the current step in the URL via
`?step=<name>`. Rules identical to tabs and filters:

- Declare an enum `FooStep` with a stable `queryValue` per step
- Read the step from
  `GoRouterState.of(context).uri.queryParameters['step']`
- Change step via
  `updateQueryParams(context, {'step': next.queryValue})`
- Omit the default step (first step)
- Form drafts (`TextEditingController`, in-progress objects) stay in
  local state with `// stateful URL skip: draft user input`
```

## Expected output

1. The inventory markdown table (step 1)
2. A / B / C classification with justification
3. Diffs of migrated wizards
4. The conformance test
5. The documentation update
6. Summary: how many wizards migrated, how many steps encoded in
   URL, how many legitimate exclusions

## Manual verification

1. Go to a wizard page, fill step 1, click Next. The URL must
   switch to `/.../create?step=<second_step_name>`.
2. Reload the page. You must stay on the second step (fields will
   be empty if local state is empty, but the active step is
   preserved).
3. Press the browser Back button. You must return to the previous
   step.
4. Copy the URL `/.../create?step=<third_step_name>`, open in a new
   tab. You must land on step 3.
5. On an intermediate step, click "Next" without filling the
   fields. The button must stay disabled (validation unchanged).
