# Changelog

## 0.1.0 — 2026-11-13

Initial public prerelease.

- Mode-aware SDR hardware control and HDR software/PWM stages.
- Serialized keypresses using one short-lived GIO application.
- One replaceable popup with a resettable 500 ms close timer.
- Notification ownership checks across restarts and manual dismissal.
- Bounded D-Bus calls, write readback and explicit failure reporting.
- Private-bus regression tests and an isolated real-GNOME notification smoke test.

Software behavior is tested. Physical brightness response, flicker and live
notification positioning remain unqualified. Device constants target the
documented Zenbook; other machines need separate qualification.
