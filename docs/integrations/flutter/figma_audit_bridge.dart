// figma_audit_bridge.dart
//
// Drop this file into your Flutter web app under `lib/` (for example
// `lib/dev/figma_audit_bridge.dart`) and call
// `FigmaAuditBridge.install(router, ...)` from `main()` when the app is
// built in audit mode. See docs/integrations/flutter/INTEGRATION.md in the
// figma-audit repository for the full setup.
//
// The bridge exposes a tiny JS surface (`window.figmaAudit`) that the
// figma-audit capture runner uses to push GoRouter routes with an `extra`
// object, read the current route, and ping for liveness. Without it,
// figma-audit can only reach pages via URL, which leaves out every page
// that depends on in-memory state passed through `context.push(route,
// extra: object)` — typically wizard steps, guest-only flows, and modal
// details.
//
// The bridge is only intended for audit/dev builds. Guard the installation
// behind a build flag (`--dart-define=FIGMA_AUDIT_ENABLED=true`) or behind
// a query parameter check (`?figma_audit=1`) so it never ships to end
// users.

import 'dart:convert';
import 'dart:js_interop';

import 'package:flutter/foundation.dart';
import 'package:go_router/go_router.dart';

/// Decoder contract: take the JSON string figma-audit sends and return the
/// in-memory object your app expects as `extra` when pushing a route.
///
/// Every page that relies on `extra: <MyObject>` must register a decoder
/// for `MyObject` so the bridge can rebuild it from JSON. Figma-audit
/// never holds real objects, only their JSON serialisation.
typedef FigmaAuditExtraDecoder = Object? Function(Map<String, dynamic> json);

/// Registers the `window.figmaAudit` JS surface so figma-audit can drive
/// the app during a capture run.
///
/// Call this once from `main()`, right after `runApp(...)` or right
/// before — the bridge does not care about ordering. It only does
/// anything in debug/profile/audit builds; in release builds the call is
/// a no-op unless you explicitly opt in.
class FigmaAuditBridge {
  FigmaAuditBridge._();

  /// Install the bridge on `window.figmaAudit`.
  ///
  /// [router] is the application `GoRouter` instance the bridge will push
  /// routes to. [extraDecoders] maps a logical type key (typically the
  /// Dart type name, e.g. `"Course"`) to a decoder that rebuilds the
  /// object from a JSON map. Only types present in this map can be passed
  /// as `extra` from figma-audit — unknown types fall back to passing the
  /// raw `Map<String, dynamic>`, which works when the receiving page does
  /// its own `Map.from(state.extra)` reconstruction.
  ///
  /// Pass [enabled] = `false` to completely skip registration (useful for
  /// release builds). By default, the bridge is installed only when
  /// `kDebugMode` or `kProfileMode` is true, OR when the build was
  /// compiled with `--dart-define=FIGMA_AUDIT_ENABLED=true`.
  static void install(
    GoRouter router, {
    Map<String, FigmaAuditExtraDecoder> extraDecoders = const {},
    bool? enabled,
  }) {
    final shouldInstall = enabled ??
        (kDebugMode ||
            kProfileMode ||
            const bool.fromEnvironment('FIGMA_AUDIT_ENABLED'));
    if (!shouldInstall) return;

    final bridge = _FigmaAuditBridgeImpl(router, extraDecoders);
    _installOnWindow(bridge);
  }
}

class _FigmaAuditBridgeImpl {
  _FigmaAuditBridgeImpl(this._router, this._decoders);

  final GoRouter _router;
  final Map<String, FigmaAuditExtraDecoder> _decoders;

  /// Push [route] onto the GoRouter stack, optionally deserializing
  /// [extraJson] into the correct Dart object via the registered decoders.
  ///
  /// The JSON envelope is expected to be either:
  ///   - `null` or empty: no extra, plain route push
  ///   - `{"__type__": "Course", "data": {...}}`: typed, uses the matching
  ///     decoder
  ///   - any other object: passed through as `Map<String, dynamic>`
  String push(String route, String? extraJson) {
    Object? extra;
    if (extraJson != null && extraJson.isNotEmpty && extraJson != 'null') {
      try {
        final decoded = jsonDecode(extraJson);
        if (decoded is Map<String, dynamic>) {
          final typeKey = decoded['__type__'];
          if (typeKey is String && _decoders.containsKey(typeKey)) {
            final data = decoded['data'];
            if (data is Map<String, dynamic>) {
              extra = _decoders[typeKey]!(data);
            } else {
              extra = _decoders[typeKey]!(<String, dynamic>{});
            }
          } else {
            extra = decoded;
          }
        } else {
          extra = decoded;
        }
      } catch (e) {
        return 'error: invalid extra JSON ($e)';
      }
    }

    try {
      _router.push(route, extra: extra);
      return 'ok';
    } catch (e) {
      return 'error: push failed ($e)';
    }
  }

  /// Return the current GoRouter location so figma-audit can confirm the
  /// navigation took effect.
  String currentRoute() {
    try {
      return _router.routeInformationProvider.value.uri.toString();
    } catch (e) {
      return '';
    }
  }

  /// Simple liveness check. Figma-audit calls this before any `push` to
  /// detect whether the bridge is installed.
  String ping() => 'ok';
}

// ─── JS interop glue ─────────────────────────────────────────────────
//
// Keep all `dart:js_interop` usage confined to this section so a Dart
// formatter pass or a future SDK upgrade only impacts one place.

@JS('window')
external JSObject get _window;

extension on JSObject {
  external void operator []=(String key, JSAny? value);
}

void _installOnWindow(_FigmaAuditBridgeImpl bridge) {
  // Build a JS object literal that mirrors the Dart methods. We allocate
  // JSFunction wrappers once so repeated calls reuse the same closures.
  final jsBridge = JSObject();
  jsBridge['push'] = ((JSString route, JSAny? extraJson) {
    final extraStr = extraJson == null ? null : (extraJson as JSString).toDart;
    return bridge.push(route.toDart, extraStr).toJS;
  }).toJS;
  jsBridge['currentRoute'] = (() => bridge.currentRoute().toJS).toJS;
  jsBridge['ping'] = (() => bridge.ping().toJS).toJS;

  _window['figmaAudit'] = jsBridge;
}
