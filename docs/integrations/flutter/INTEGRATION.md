# Flutter integration guide

This document explains what a Flutter web application has to add in order
to be fully auditable by [figma-audit](https://github.com/sbassomp/figma-audit).

Two small changes are needed. Apply both at the same time. Together they
let figma-audit capture every page of the app, including:

- pages reachable only via UI interactions (wizards, detail buttons)
- pages that take an in-memory `extra` object through GoRouter
- pages that only render for a specific app state (logged-out guest flow,
  taken course, paid order, etc.)

The integration is compile-time optional. In release builds the app
behaves exactly as before. In audit/debug builds, two extra touch points
are exposed that figma-audit uses to drive the app.

---

## Change 1: turn on Flutter Semantics (one line)

Flutter Web ships two rendering modes, HTML and CanvasKit. CanvasKit is
faster and matches mobile visually but it renders everything into a
`<canvas>` element. Without extra setup, the browser DOM contains no
widgets, no buttons and no labels, so figma-audit cannot locate UI
elements to click or fill.

Flutter has a parallel accessibility tree called **Semantics** which,
when enabled, mirrors the widget tree as hidden `<flt-semantics>` DOM
nodes. Playwright can then query these via `getByRole`, `getByLabel`
and friends. Enabling it is one line.

**Edit `lib/main.dart`:**

```dart
import 'package:flutter/material.dart';
import 'package:flutter/semantics.dart';

void main() {
  // Enable the Flutter accessibility tree so automated audits and
  // assistive technologies can locate widgets by role/label.
  SemanticsBinding.instance.ensureSemantics();

  runApp(const MyApp());
}
```

That is the entire change. No widget tree refactor, no additional
packages, no behaviour change at runtime. The `<flt-semantics>` nodes
are transparent to end users and already rely on the `Semantics` widgets
the Flutter framework inserts around buttons, text fields, and text.

> Tip: if you have custom gesture detectors that wrap interactive
> elements but do not use `Semantics` (for example a `GestureDetector`
> wrapped around a styled `Container`), wrap them in
> `Semantics(button: true, label: 'Label that matches the visible text', child: ...)`.
> Without a matching label, figma-audit cannot target them by text.

---

## Change 2: install the figma-audit bridge (≈50 lines)

The Semantics tree is enough to handle click/fill-based navigation. It
is **not** enough for pages that are only reachable via
`context.push(route, extra: someObject)` — the `extra` field is an
in-memory Dart object which cannot be serialised into a URL and therefore
cannot be reached by `page.goto(...)`.

Figma-audit solves this with a small JS bridge the app registers on
`window.figmaAudit`. It exposes three methods:

- `push(route, extraJson)` — calls `router.push(route, extra: <decoded>)`
- `currentRoute()` — returns the current location
- `ping()` — liveness probe figma-audit uses to detect the bridge

### Step 1: add the bridge file to your project

Copy [`figma_audit_bridge.dart`](./figma_audit_bridge.dart) into your
project under `lib/dev/figma_audit_bridge.dart` (or any path that reflects
"this is a dev/audit helper").

### Step 2: install it from `main()`

After the `SemanticsBinding.instance.ensureSemantics()` line, install the
bridge and pass it your `GoRouter` instance plus a decoder map for every
type used as `extra`:

```dart
import 'package:flutter/material.dart';
import 'package:flutter/semantics.dart';

import 'dev/figma_audit_bridge.dart';
import 'features/courses/domain/entities/course.dart';
import 'router/app_router.dart'; // or wherever your GoRouter lives

void main() {
  SemanticsBinding.instance.ensureSemantics();

  FigmaAuditBridge.install(
    appRouter, // your shared GoRouter instance
    extraDecoders: {
      // One entry per type you pass as `extra` through GoRouter.
      'Course': (json) => Course.fromJson(json),
      // Add more as your app grows:
      // 'Invoice': (json) => Invoice.fromJson(json),
      // 'Booking': (json) => Booking.fromJson(json),
    },
  );

  runApp(const MyApp());
}
```

`FigmaAuditBridge.install` is only active in debug/profile builds, or when
the build was run with `--dart-define=FIGMA_AUDIT_ENABLED=true`. In
standard release builds the call is a silent no-op, so it is safe to
leave in permanently.

### Step 3: audit the `extra` call sites and add decoders

Figma-audit can only call a decoder that you have registered. Do this
once per new `extra` type: `grep` your codebase for `extra:` to find the
live set of types.

```bash
grep -rn 'extra:' lib/ | grep -v '//'
```

Each result like `context.push('/foo', extra: <MyType>(...))` means you
need an entry `'MyType': (json) => MyType.fromJson(json)` in the decoder
map. Missing entries degrade gracefully: figma-audit passes through a
raw `Map<String, dynamic>` which may be enough if the receiving page
does its own reconstruction, but explicit decoders give the best
fidelity.

### Step 4 (optional): guard non-audit environments

If you are paranoid about shipping the bridge unintentionally, wrap the
install call behind an explicit env var:

```dart
const figmaAuditEnabled =
    bool.fromEnvironment('FIGMA_AUDIT_ENABLED', defaultValue: false);

if (figmaAuditEnabled) {
  FigmaAuditBridge.install(appRouter, extraDecoders: { /* ... */ });
}
```

Then build the audit variant with:

```bash
flutter build web --dart-define=FIGMA_AUDIT_ENABLED=true
```

---

## Verifying the integration

After deploying the changes to a staging build, open the devtools
console on any page of your app and run:

```js
window.figmaAudit && window.figmaAudit.ping()
// expected: "ok"
```

To test Semantics is really on, search the DOM for `flt-semantics`:

```js
document.querySelectorAll('flt-semantics, flt-semantics-host').length
// expected: a positive number once the first frame has rendered
```

If both checks pass, the app is fully auditable and figma-audit will
stop tripping over URL-only navigation or missing accessibility roles.

---

## Frequently asked

### Does enabling Semantics slow down the app?

Measurably? No. The Semantics tree is built on demand by Flutter's
accessibility framework. Users who run a real assistive technology
(screen reader, switch control) already trigger this code path. The
bridge adds one JS object to `window` at startup. Neither impacts
rendering performance.

### Does the bridge expose anything sensitive?

The bridge only calls `router.push(route, extra: object)` with data
provided by the caller. It reads the current route. It does not touch
auth, state, storage, or the network. If the audit build is restricted
to staging, the bridge cannot be used maliciously in production.

### What about apps that do not use GoRouter?

The bridge expects a `GoRouter` instance. For `Navigator 1.0` apps or
custom routers, adapt the `_FigmaAuditBridgeImpl.push` method to call
your navigator instead. The JS surface stays the same, so figma-audit
does not need to know about the swap.

### Can I deploy the bridge to production?

You can, but there is no reason to. Keep it limited to audit/debug/staging
builds to minimise surface area. The default install guard
(`kDebugMode || kProfileMode || FIGMA_AUDIT_ENABLED`) already does this
correctly.
