# feat(shp): add per-circuit power monitoring, circuit mode selects, and utility buttons

## Summary

Adds per-circuit power monitoring, per-circuit mode control (Auto/Grid/Battery/Off), aggregated power demand sensors, and utility buttons to the Smart Home Panel (public API).

## Problem

The SHP integration exposes battery-level and grid-level metrics but lacks visibility into individual circuit breakers. Users cannot:
- See per-circuit wattage or determine whether each circuit is drawing from grid vs battery
- Control individual circuit modes (Auto/Grid/Battery/Off) from HA
- View aggregated demand broken out by power source
- Trigger device maintenance operations (RTC sync, self-check)

## Solution

Two new helper classes and additive sensor/select/button definitions — no existing entities or IDs are modified.

### New classes

- **`CircuitModeSelectEntity`** — Combines `ctrlMode` (0=Auto, 1=Manual) and `ctrlSta` (0=Grid, 1=Battery, 2=Off) into a single user-friendly select with options Auto/Grid/Battery/Off. Sends `cmdSet: 11, id: 16` commands.
- **`AggregatedWattsSensorEntity`** — Subclasses `WattsSensorEntity`, computes a value from the `infoList` array via a caller-provided aggregator function. Uses a synthetic `unique_key` to avoid ID collisions.

### New entities

**Sensors (enabled by default):**
- Breaker 1–10 Power (W) — per-circuit wattage with attributes: Power Source, Circuit State, Control Mode
- Breaker 1–10 Battery Power (W) — wattage only when circuit draws from battery
- Breaker 1–10 Grid Power (W) — wattage only when circuit draws from grid
- Battery 1/2 Power (W) — infoList indices 10/11
- Circuits Combined Power (W) — sum of all 10 circuit breakers
- Circuits Battery Demand Power (W) — sum of battery-sourced circuits
- Circuits Grid Demand Power (W) — sum of grid-sourced circuits
- Battery Combined Power (W) — sum of both battery slots
- Battery 1/2 Level attributes: Connected, Enabled, Grid Charging, Discharge Time, Charge Time, Output Power

**Selects:**
- Circuit 1–10 Mode (Auto/Grid/Battery/Off)

**Buttons (disabled by default):**
- Update Real-Time Clock — syncs device RTC to current time

### Constant added
- `BREAKER_N_POWER = "Breaker %i Power"` in `const.py`

## Files changed

- `custom_components/ecoflow_cloud/devices/public/smart_home_panel.py` — helper classes + extended `sensors()`, `selects()`, new `buttons()`
- `custom_components/ecoflow_cloud/devices/const.py` — 1 new constant

## Backward Compatibility

- **No breaking changes** — all existing sensors, switches, numbers, and selects remain identical
- **No entity-ID migration** needed for existing installs
- New entities appear automatically after update
- RTC button is disabled by default to avoid accidental triggers

## Dependencies

None — this PR applies cleanly on top of current `main`.

## Testing

- Tested on real Smart Home Panel hardware (SHP model with 2× Delta Pro batteries, 10 circuits active)
- Running stable in a downstream fork since early 2025
- Circuit mode selects verified bidirectionally (HA ↔ EcoFlow app)
- Aggregated power sensors validated against EcoFlow app totals
- Buttons tested: RTC sync confirmed via app clock display, self-check triggers correctly
- No regressions to existing battery level, temperature, charge/discharge, or scheduled charge entities
