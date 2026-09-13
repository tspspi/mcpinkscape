# Design for Inkscape

This is original operating guidance for use with `mcpinkscape`. It teaches an
agent how to turn a communication brief into an inspectable Inkscape document;
it is not a license to reuse a reference image, logo, typeface, template, or
third-party design asset.

## Route the task

| If the task is primarily about... | Use these sections |
| --- | --- |
| Any designed communication | Brief, design rules, Inkscape workflow |
| Promotional/event poster | Promotional poster profile, diagnostic examples |
| Academic or scientific poster | Scientific poster profile, scientific evidence |
| Leaflet, brochure, or fold | Folded/publication profile, then delivery review |
| Presentation folder or other manufactured piece | Manufactured-object profile, then delivery review |
| Digital companion | Digital companion profile, then delivery review |
| A render that feels weak or crowded | Design rules and diagnostic examples |

## Start with an actionable brief

Before laying out a substantial piece, collect the following. Mark an unknown
item as an assumption, not as an implicit approval.

- **Reader and encounter:** who reads it, what they already know, language,
  access needs, distance, time available, and whether a presenter is present.
- **Communication:** one main takeaway or action, supporting messages, tone,
  required claims, required names/dates/contacts/citations, and what must be
  found quickly.
- **Format:** finished and flat dimensions, units, pages/panels, binding or
  fold sequence, physical states, and required exports.
- **Assets:** approved text, figures/data, raster sources, rights/provenance,
  brand constraints, and which content may be edited or cropped.
- **Production:** actual printer/vendor specification, template revision,
  safe regions, finishing, colour/output requirements, and required proof.
- **Scientific content:** claim-to-source mapping, figure/data provenance,
  mandatory units/legends/uncertainty, and needed domain review.

If production information is missing, make a labeled concept draft. Do not
invent it or claim that the result is print-ready.

## Design rules

### 1. Make the reader's route visible

Choose one entry point: a headline, a key image, a question, a finding, or a
clear action. Establish what the reader sees first, what verifies it next, and
where detail lives. A title is not automatically the main message.

Use contrast in scale, weight, position, colour, whitespace, and grouping with
purpose. If everything is prominent, nothing is. Render the page and ask what
a reader could correctly say after a short glance; then test whether a slower
reader can locate the supporting evidence or practical details.

### 2. Let content determine the structure

Use a grid, axis, visual field, or other deliberate organizing system. Align
related items, repeat spacing relationships, and leave real breathing room.
Asymmetry, illustration-led composition, dense technical diagrams, and
editorial variation are valid when they improve the brief; uniform cards and a
single accent colour are not default evidence of quality.

When content overflows, first reconsider priority, wording, grouping, page
size, or the need for a companion. Do not silently shrink all type, remove a
caption, or crop evidence merely to make a layout fit.

### 3. Use typography as a system of roles

Define roles such as display/title, section heading, body, caption, label,
annotation, citation, and contact information. Give each a deliberate visual
relationship rather than selecting sizes independently. Judge the rendered
glyphs at the intended reading distance: apparent size, line length, leading,
contrast, language, and actual physical scale all matter.

Keep text selectable SVG text when practical. Check line breaks, hyphenation,
punctuation, symbols, and substituted glyphs in the render. Captions, axes,
legends, and contact paths are reader content, not expendable decoration.

### 4. Make colour and images carry meaning

Use colour to distinguish roles, states, categories, or emphasis; do not make
meaning depend on colour alone. Keep sufficient tonal distinction for the
actual medium and background. A screen contrast calculation can help a digital
companion, but it is neither a print proof nor a complete accessibility claim.

Place raster images at a deliberate crop and scale. Preserve the distinction
between an observation, a simulation, a diagram, and a decorative illustration.
Use `import_image` only for approved configured-root assets; it embeds the
verified source by default. Do not fabricate a measurement image, institutional
logo, data point, citation, or scientific result.

### 5. Build for inspection and revision

Give layers clear roles: for example `structure`, `text`, `figures`,
`annotations`, and temporary `construction`. Use stable IDs for important
objects. Keep temporary construction geometry separate and remove it before a
final export unless it is intended artwork.

Use typed shape/text/style/transform tools. Graphical feedback is required:
render after composition, after every consequential typography/figure/style or
transform change, and before delivery. Inspect the page render for hierarchy
and the relevant detail render for small text or scientific evidence. A render
is evidence of the document pixels, not proof of a physical print,
screen-reader result, or printer acceptance.

## Format profiles

### Promotional or event poster

Design for a fast entry: subject or event, the most useful action, then the
facts needed to act. Test whether date, place, time, contact/booking route, and
identity can be found without reading every block. A cultural or artistic
poster may deliberately prioritize mood over immediate logistics; the brief
decides that tradeoff.

Use a main visual anchor and protect its space. Put factual details into an
easy-to-scan secondary region. Verify content accuracy separately from visual
appeal.

### Attended scientific or academic poster

Design for a conversation. State a defensible contribution or question early,
then make the supporting evidence visible enough that a visitor can choose
where to ask questions. Reserve room for spoken explanation rather than
turning the poster into a paper transcript.

The contribution may be a method, theory, negative result, limitation, or
work in progress. Do not force a marketing-style positive conclusion. Figures
need an interpretable visual role: a reader should be able to identify what is
measured or compared, what labels/units mean, and what the figure supports.

### Standalone scientific or explanatory poster

Make the narrative more self-contained than for an attended session. Supply
context, definitions, captions, and a reading route that do not depend on the
author being present. Prefer a few well-explained figures to many tiny ones.
If a conclusion relies on qualifications or uncertainty, make that visible at
the point where the reader interprets the evidence.

### Folded leaflet or brochure

Model physical panels and reader order separately. A cover is encountered
closed; an interior is encountered after an opening action. Do not infer final
panel order from a row of rectangles on screen. Put essential content away from
unconfirmed fold/trim risks, and render or print a flat proof only after the
actual panel map is known.

A brochure is a sequence, not a single poster split into pages. Establish what
each page/panel contributes, preserve navigation and rhythm, and distinguish
reader pages from printer imposition. Do not impose a handoff file unless the
printer specifically requests it.

### Presentation folder or other manufactured object

Use the exact vendor dieline and its revision. Pockets, flaps, glue areas,
cut/crease lines, and assembled orientation are geometric constraints, not
decoration. Keep required branding and contact information visible after
assembly. Without the actual dieline and process specification, deliver a
concept only, not a manufacturing-ready file.

### Digital companion

Treat it as a separately usable deliverable, not merely a rasterized print
page. Plan reading order, selectable text, text alternatives, link behavior,
and zoom/reflow expectations. Inkscape SVG/PDF export alone does not establish
semantic reading order, tagged-PDF quality, or assistive-technology support;
state those checks separately.

## Scientific evidence and figure rules

- State the question, comparison, or contribution before optimizing visual
  style. Do not alter a figure in a way that strengthens an unsupported claim.
- Preserve provenance. Label conceptual art and simulation distinctly from
  acquired data. Preserve source references and domain review requirements.
- Match encoding to meaning: ordered magnitude needs an ordered treatment;
  a reference-centered quantity needs a reference-visible treatment; categories
  need distinguishable categories; cyclic values need a cyclic treatment.
- Retain labels, units, legends, scale information, and uncertainty whenever
  they are necessary to interpret the representation. Not every figure needs
  the same apparatus; let the science decide.
- Review figures at final placed size. Tiny labels may pass an object-inspection
  check while failing the reader's actual viewing condition.
- Obtain domain review for changes to measurements, microscopy, contrast,
  colormaps, normalization, annotations, or interpretation that could affect a
  scientific conclusion.

## Annotated diagnostic examples

These are original failure-pattern examples, not templates to copy.

| Rendered symptom | Likely reader failure | Inkscape correction | Verification |
| --- | --- | --- | --- |
| Every heading, image, and coloured box competes | No clear first message | Reduce competing emphasis; enlarge or reposition the true entry point; group supporting items | Render the whole page and identify the first, second, and third reading stops |
| A poster has long paragraphs and tiny figures | Visitor cannot identify the contribution or read evidence | Convert the message into a short claim/question; move detail into captions, callouts, or a companion; enlarge key evidence | Inspect page render and a detail render of every figure/label |
| A data plot is attractive but ambiguous | Reader cannot tell what changes or what colours mean | Add concise axes/units/legend; choose a scale that exposes the relevant reference or ordering | Ask whether the displayed encoding supports the stated claim without explanation |
| A leaflet works as a flat canvas but fails when folded | Cover, back, or opening sequence is wrong | Create explicit panel rectangles and labels on a construction layer; map physical and reading states; revise content placement | Review closed, first-open, and fully-open states against the vendor specification |
| A contact route exists only as a QR code or tiny URL | Reader without a scanner or close view cannot act | Add readable contact text and protect it from visual competition | Inspect at the intended distance and verify the literal contact string |
| An image is enlarged until soft or misleading | Detail cannot be trusted at final output | Use an approved higher-resolution/cropped source, reduce placed size, or replace with a diagram | Calculate effective resolution for the final physical placement and inspect the export |

## MCP execution pattern

1. Call `server_status`; create/open a document with the correct units and
   dimensions. Read the current revision.
2. Create named layers and build major regions with typed rectangles, lines,
   shapes, text, and imported approved assets. Keep construction objects
   separable from content.
3. Use `set_fill`, `set_stroke`, gradients, opacity, transforms, grouping, and
   stacking to establish hierarchy. Avoid one-off style changes when a role
   style can be applied consistently.
4. Inspect object bounds and render a page snapshot after each consequential
   edit. For a dense figure or caption, also inspect the relevant detail
   through the available document workflow; do not accept an edit from object
   metadata alone.
5. Record the diagnosed issue, revise only what addresses it, render again,
   then save/export after the appropriate delivery checks.

Read [delivery and review](design-review.md) before calling a result final,
print-ready, accessible, or production-approved.
