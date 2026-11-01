# Native alignment and distribution

`object.align` and `object.distribute` operate on one explicit `instance_id` /
`document_id` target at one `expected_revision`. Their MCP tools are
`live_align_objects` and `live_distribute_objects`. Both also work as entries in
`document.batch` / `live_apply_batch`, including references to earlier creations.
They require a native bridge advertising the corresponding methods.

All calculations use axis-aligned Inkscape bounds in document CSS pixels, with a
top-left origin, x right and y down. `bounds` is `geometric` (default) or `visual`
(includes Inkscape's stroke/filter bounds). Text uses laid-out native bounds.
Translation is converted through each parent's affine, preserving nested groups,
rotation, scale, and stroke widths. Drawing-tool coordinates can instead be in
parent SVG user units; do not confuse them with layout's document pixels.

## Alignment

Parameters:

- `object_ids`: 1–1000 unique drawable object IDs.
- `axis`: `x` or `y`.
- `alignment`: `start`, `center`, or `end`. Start/end mean left/right on x,
  top/bottom on y.
- `reference`: `selection` (default), `page`, or `object`.
- `reference_id`: required only for `object`; the fixed reference is not moved.
- `bounds`: `geometric` or `visual`.

`selection` uses the union of the requested objects' bounds at the start of the
operation, independent of GUI selection. `page` uses the root SVG viewport from
(0,0) to the physical document width/height in CSS pixels. It does not choose a
page from a multi-page document or follow the active desktop page. `object`
uses another visible drawable object's bounds, even if that reference is locked.
The perpendicular coordinate remains unchanged.

## Distribution

Parameters:

- `object_ids`: at least 3 IDs, or at least 2 with an explicit gap; maximum 1000.
- `axis`: `x` or `y`.
- `spacing`: `gaps` (default) or `centers`.
- `gap`: optional finite nonnegative edge gap; only valid for `gaps`.
- `unit`: `px` (default), `mm`, `cm`, `in`, `pt`, or `pc`; non-pixel units require
  an explicit gap. Conversions use 96 CSS pixels per inch.
- `bounds`: `geometric` or `visual`.

Objects are stably sorted by their leading bounds edge for gaps, or bounds
center for center spacing. Equal positions retain input order. There is no
reordering in the SVG tree. Without `gap`, the first and last objects stay fixed
and intermediate objects receive equal edge gaps or center distances. Automatic
edge gaps may be negative when the available span requires overlap. With `gap`,
the first object stays fixed and all subsequent objects move to that edge gap;
the final object may move beyond the previous span or page. No snapping or
rounding to a document grid is applied.

## Transactions and receipts

The complete operation is prevalidated on a detached document before live edits.
Both individual tools and batch entries share the atomic batch transaction:
one revision and undo step for changes, rollback on execution failure, and
unchanged history/revision for a no-op. Selection, current layer, window focus,
and SVG tree order are preserved. Stale targets/revisions fail; do not replay
an operation automatically after losing its response.

Missing/hidden/unbounded objects, locked movers (including locked ancestors),
root/non-drawable targets, duplicate IDs, overlapping ancestor/descendant movers,
and reference objects equal to or ancestral/descendant to a mover are rejected.
A parent transform that cannot be inverted for the required movement is rejected.
Movement differences within 1e-9 document pixels are treated as zero.

Tool receipts include `changed`, `revision`, affected IDs, and `moves` with each
object's `dx`, `dy`, and `before`/`after` bounds in document pixels. They also
include `bounds`, `bounds_space`, `axis`, and `resolved_spacing_px` (edge gap or
center distance; null for alignment). Native protocol responses use the batch
receipt shape with one `operation_results` entry; the MCP individual tools also
expose that entry's fields at the top level. In multi-operation batches, receipts
are found in `operation_results` and describe each operation's intermediate state.
Inspect the final geometry and render with the same document target.

Example batch for three panels with aligned tops and an 8 mm visible edge gap:

```json
[
  {"method":"object.align","params":{"object_ids":["left","middle","right"],"axis":"y","alignment":"start","bounds":"visual"}},
  {"method":"object.distribute","params":{"object_ids":["left","middle","right"],"axis":"x","gap":8,"unit":"mm","bounds":"visual"}}
]
```


## Automated verification

With an ABI-matched installed Inkscape and Xvfb, gdbus, dbus-run-session and
libXtst available, run the focused private-display checks:

```sh
python3 packaging/verify_mcp_lifecycle.py /path/to/inkscape \
  --layout-only --output /tmp/mcpinkscape-layout.json
```

Omit `--layout-only` for the full lifecycle/target/inspection/batch suite, which
also includes layout. The layout fixture checks analytical bounds, rendered
pixels, transformed groups/text, physical units, focus/selection preservation,
stale and malformed requests, grouped undo/redo and injected live rollback.
