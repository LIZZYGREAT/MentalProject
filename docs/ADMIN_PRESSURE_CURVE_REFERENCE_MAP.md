# Admin Pressure Curve Reference Map

## Source audit scope

The local reference-only directory is named `referrence/` (double `r`). There is no
tracked `reference/` directory in this repository. The files below were inspected
as design and transport references only; production Admin code does not import or
package them.

| Reference file | Reference function or view | Current implementation |
| --- | --- | --- |
| `referrence/visualization/plotter.py` | `_draw_core_plot` | `mindflow-bot-runtime/app/services/pressure_curve_renderer.py::PressureCurveRenderer._draw_core_plot` |
| `referrence/visualization/plotter.py` | `get_plot_image_base64` | `PressureCurveRenderer.render` returns PNG bytes; `PressureCurveService.read_persisted` supplies the persisted Forecast data |
| `referrence/frontend/src/views/HomeView.vue` | `runPrediction`, `chartSource` | `mindflow-bot-runtime/app/admin_web/api.py::AdminAPI.pressure_curve_image` and `app/admin_web/static/app.js::pressureCurveImage` |
| `referrence/frontend/src/views/HomeView.vue` | `<img v-if="chartSource" ...>` | Admin renders `/admin/api/participants/{participant_code}/pressure-curve/{local_date}.png` with an `<img>` element |

## Visual and transport semantics retained

- Matplotlib uses the non-interactive `Agg` backend and emits PNG output.
- The normal view uses two vertically stacked panels with a shared time axis and
  the configured `S_PANEL_HEIGHT_RATIO` / `E_PANEL_HEIGHT_RATIO`.
- Stress remains a royal-blue trajectory with a baseline, attention threshold,
  warning markers, and an optional confidence axis.
- Calendar events are rendered as translucent spans on both panels, with
  alternating label lanes and event-type colors.
- The chart uses a white background, restrained dashed grid, CJK-capable font
  selection, rotated 24-hour labels, and an approximately 16:9 canvas.
- The browser receives a server-rendered PNG and displays it as an image. The
  Admin route uses raw PNG bytes rather than the reference Base64 wrapper.

## Current model-aware extensions

- The renderer uses the persisted Forecast and active M0–M3 model context rather
  than running a new simulation while reading the chart.
- A 90% stress interval and dynamic equilibrium are rendered only when those
  fields exist in the persisted Forecast.
- The second panel follows the active model: dynamic vitality for models that
  compute it, otherwise event/anticipatory/post-event inputs. It does not invent
  an energy state.
- Fatigue/penalty shading is displayed only when the active model has the fatigue
  state.
- Unknown high-importance events receive an auditable high-importance style;
  other unknown events use a neutral style.
- The current-time marker is hidden for historical dates.

## Legacy behavior explicitly not migrated

- `GLOBAL_DEFAULT_CONFIG` and reference-side fallback parameters are not used as
  an Admin data source.
- Missing energy is not replaced with `DEFAULT_INITIAL_ENERGY`.
- Reference default thresholds do not override the active persisted model
  context.
- The reference `/api/simulate` workflow, SQLite reads, and Base64 JSON response
  contract were not copied. Admin reads the authoritative persisted Forecast and
  serves `image/png`.
- Reference event object methods, blanket exception suppression, and unconditional
  `f_pen` rendering were not copied.
- The reference frontend's simulation/recalculation behavior was not migrated;
  historical Admin Forecast reads remain read-only unless an administrator uses
  the existing explicit refresh action.
