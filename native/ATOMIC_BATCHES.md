# Native atomic edit batches

`document.batch` is an additive protocol-v1 method. The request requires an
explicit `target` containing both instance/document UUIDs and one non-negative
integer `expected_revision` within the exact JSON integer range (0–2^53−1). `params` contains only `operations`, an array of
1–100 entries. The complete UTF-8 request must fit within 256 KiB.

Each operation is `{ "method": "…", "params": { … } }`. It cannot change the
target or revision, invoke another batch, control the application, render, run
arbitrary actions, or supply raw XML. Supported methods are:

- `shape.create`, `text.create`, `text.set`, `layer.create`
- `object.set_style`, `object.move`, `object.rotate`, `object.scale`
- `object.delete`, `object.group`, `object.ungroup`, `object.raise`, `object.lower`
- `gradient.create`, `gradient.set_stops`, `gradient.apply`
- `document.set_background`

Parameters use the corresponding standalone protocol method's fields. Creation
requires explicit object/layer/gradient IDs; later operations can refer to these
IDs. Duplicate target IDs, overlapping ancestor/descendant delete targets, and
structural edits to the SVG root or non-drawable metadata are rejected. Native
image import, duplication, selection, and destination-layer changes remain
standalone operations.

The GUI callback resolves the target and checks its revision, then validates the
whole sequence by applying it to a detached document copy. The live document is
edited only after every operation succeeds there. The batch uses the initial
current layer as its default destination; an explicit layer ID can identify a
layer created earlier in the batch.

Live execution is serialized within the same GUI callback. Per-operation undo
and revision commits are deferred. Batch-only XML events are journaled so an
execution failure can undo those events without replacing the document or
clearing an existing redo stack. Rollback restores selection, destination layer,
modified flags, and collaborative revision/selection-generation state. A successful
batch preserves surviving selected IDs and the destination layer when it still
exists; deleting that layer selects the SVG root as the new default.

Success returns actual target IDs, a `changed` flag, the resulting `revision`,
`operation_count`, ordered `operation_results`, and the union of `affected_ids`
from those receipts. Each receipt includes
explicit operation targets as well as IDs returned by the native mutation. One
native undo/redo step reverses/reapplies a batch with changes, which advances the
revision once. A no-op returns `changed: false` and preserves revision and history.
A successful new edit follows Inkscape's normal behavior of discarding redo history.

`batch_validation_failed` and `batch_execution_failed` errors include a zero-based
`operation_index` and a message. The latter reports that changes were rolled back.
Stale revisions and targets retain their usual error codes. Transport timeout or
loss of the response does not prove failure: the batch may already have committed.
Never replay automatically; inspect the same document and its revision first.

For isolated developer verification only, the native application environment can
set `MCPINKSCAPE_TEST_BATCH_FAIL_AFTER` to an operation count. This injects an
exception after that many live operations. It is not an MCP argument or tool and
must not be enabled for ordinary editing.
