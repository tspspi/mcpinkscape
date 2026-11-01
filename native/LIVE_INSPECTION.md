# Native inspection and layers

Protocol-v1 bridges advertise the following methods individually. All accept the
optional document target described in [DOCUMENT_TARGETING.md](DOCUMENT_TARGETING.md).

| Method | Parameters | Result fields beyond target identity |
| --- | --- | --- |
| `document.info` | none | revision, root_attributes, coordinates, current_layer_id |
| `object.inspect` | object_id | revision, coordinates, object |
| `layer.list` | offset, limit (1–1000) | revision, total, layers |
| `layer.create` | layer_id, label, optional parent_layer_id; expected_revision in envelope | revision, changed_ids |
| `layer.set_current` | layer_id; expected_revision in envelope | revision, current_layer_id |

Object inspection includes ID, SVG type, parent ID, child IDs, attributes, layer
flag, affine transforms, geometric and visual bounds, and effective paint and
basic typography. Text includes content and layout-derived glyph bounds.
Non-item objects have null transforms/bounds. An absent geometric bound is null,
not a zero-sized box. Inspection results over 512 KiB fail explicitly.

Bounds are `{x,y,width,height}` in document pixels. `item_to_document` and
`root_to_document` are six-element SVG affine matrices, including the root
viewBox conversion. The page width/height use CSS pixels (96 per inch), with a
top-left origin, positive x rightward and positive y downward. `local_transform`
maps object-local coordinates into its parent. Drawing values use destination
parent user space; local stroke width and font size retain those user units.

Effective style reports inherited fill/stroke, fill/stroke opacity, local
opacity, cumulative ancestor opacity, local stroke width/font size, font family,
and computed font weight. It is a supported subset of CSS, not a complete
browser computed-style API. Paint servers/context paint remain CSS references.
Visual bounds follow Inkscape's stroke, filter, clip, and mask calculations.

Layers are traversed in document order, including layers nested inside ordinary
groups. Parent IDs identify the nearest ancestor layer. Hidden and locked flags
describe each layer itself; ancestor flags still apply. Creation defaults to the
SVG root; an explicit parent must be a layer. Duplicate IDs and invalid parents
fail before mutation. A successful creation is one undoable document revision.

Setting the current layer changes the targeted desktop's destination without
changing focus or selection, modifying SVG, or adding an undo entry. It checks
the document revision before changing the destination. Shape/text creation can
also supply `layer_id`; image import follows the current destination layer.
