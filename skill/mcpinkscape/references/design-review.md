# Delivery and Review for Inkscape Work

Use this original checklist after creating a designed document with
`mcpinkscape`. It separates concept quality, document/render checks, and
production evidence. It does not turn an Inkscape export into a claim that a
printer, conference, or accessibility reviewer has approved it.

## Declare the delivery stage

Use a truthful label:

| Stage | What can be claimed |
| --- | --- |
| `concept` | Direction, hierarchy, and content placement have been explored; production inputs may be assumed or absent. |
| `reviewable` | The document has been rendered and checked against known content/format constraints. |
| `production_candidate` | Known vendor/conference requirements were applied and the requested export was inspected; external validation may still remain. |
| `approved` | Use only when the required external approval or physical proof is actually recorded. |

Do not claim print-ready, accessible, tagged-PDF, PDF/X, colour-managed, or
physically proofed status without the specific required evidence.

## Inkscape review loop

1. Save the editable SVG and retain the document revision.
2. Render a PNG snapshot of every page/drawing after each consequential edit,
   and again before delivery. Inspect overview hierarchy, intended reading
   scale, and important details such as labels, captions, and contact
   information.
3. Check object structure: required text/figures are present, no temporary
   construction layer survives unintentionally, bounds do not clip essential
   content, and imported raster assets are the approved files.
4. Export the requested SVG, plain SVG, PNG, or PDF with `export_document`.
   Inspect the exported result separately; source inspection is not enough.
5. Make a specific correction for every failure, then render/export again.

`render_snapshot` proves only the rendered document image. It is not an
operating-system screenshot, a calibrated physical-size proof, or a substitute
for a production preflight tool.

## Checks by concern

| Concern | Check now with Inkscape/MCP | Keep open or validate externally |
| --- | --- | --- |
| Content | Required names, dates, labels, contacts, citations, and claims are present and readable in a render | Factual approval, legal review, and domain review |
| Layout | Bounds, clipping, overlap, alignment, hierarchy, and text at viewing scale | Reader comprehension or audience testing |
| Raster figures | Correct source, intentional crop, final placed size, readable labels | Required effective resolution and print reproduction on the chosen process |
| Fold/binding | Page dimensions and a supplied panel map/dieline are represented | Vendor confirmation, imposition, fold/assembly proof |
| Print | Requested PDF/PNG/SVG is exported and visually inspected | Printer job ticket, profile, separations, marks, preflight, press proof |
| Digital accessibility | Selectable text is retained where possible; visual contrast and reading route are reviewed | Tagged PDF/SVG semantics, assistive-technology behavior, formal conformance |
| Scientific content | Claim/evidence relationship, labels, units, figure provenance, and limitations are visible | Subject-matter review and data/interpretation approval |

## Production constraints are job-specific

Use the actual printer, vendor, or conference specification. Do not assume a
universal bleed, colour mode, PDF preset, raster resolution, mark set, or
imposition scheme. Keep cut/crease/glue/finishing instructions outside visible
artwork unless the specified workflow deliberately uses named production
separations.

For a raster asset, assess resolution at final placement rather than trusting
embedded DPI metadata:

```text
effective_ppi = pixels along an axis / placed inches along that axis
```

Compare each axis with the actual job requirement for that image type and
viewing condition. Inkscape/MCP can place, render, and export the asset, but
does not itself certify press suitability, ICC handling, PDF/X, PDF/UA, or
vendor acceptance.

## Review questions

Use reader tasks instead of ungrounded praise:

- What is the main message after a brief glance?
- Can the reader find the next action, event fact, contact path, or section?
- For a scientific piece, what does each key figure support, and what remains
  uncertain or outside its scope?
- Does the reading route match the physical state: closed, opened, paged, or
  viewed from a distance?
- Which exact object causes a failure, and which specific change should help?

If possible, gather a human/domain/production review appropriate to the risk.
Keep disagreement and unresolved assumptions visible rather than averaging
them into a confident final status.

## Compact delivery record

Keep a short record with the source document, exports, and snapshots:

```text
stage: reviewable
confirmed_constraints: [page size, required text, supplied assets]
assumptions: [printer profile not supplied]
checks:
  - content: pass (rendered page checked)
  - hierarchy: pass (main message visible at overview)
  - figure labels: fail (detail render shows unreadable axis labels)
  - printer preflight: not_run (no job ticket or validator)
next_action: enlarge figure labels and re-render
```

Use `pass`, `fail`, `not_run`, or `not_applicable` with a reason or evidence.
A favorable visual review cannot cancel a failed integrity or production check.
