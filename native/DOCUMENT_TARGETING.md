# Document targeting (protocol v1 extension)

`bridge.hello` advertises `document_targeting: true`, `instance_id`, and
`document.list` when explicit targeting is supported. Older protocol-v1 bridges
remain usable for legacy focus-based calls; clients must reject explicit targets
when this capability is absent.

`document.list` returns `instance_id` and `documents`. Each document contains
`instance_id`, `document_id`, `label`, `modified`, `desktop_available`, `revision`,
and `selection_generation`. IDs are random UUIDs, unrelated to paths or SVG IDs.
The instance ID lasts for the native application process. A document ID lasts
until that document is destroyed. Bridge Stop/Start and MCP reconnection preserve
both; reopening a file or restarting the application invalidates the old handle.

Document, object, shape, text, gradient, image, selection, and `snapshot.render`
requests can include this envelope member:

```json
"target": {
  "instance_id": "4dc958eb-a9c3-4da3-a68b-35d91c794125",
  "document_id": "84873af8-f507-438d-8256-8aee94f27ad2"
}
```

The GUI callback resolves the target before reading state or checking
`expected_revision`. It chooses a desktop belonging to the document without
changing focus. A mismatched instance or destroyed document returns
`stale_document_target`; an open document without a desktop returns
`document_not_displayed`. Neither case selects a substitute document.
Successful document receipts include both IDs. Snapshot receipts identify the
document serialized at acquisition even if focus changes during rendering.
`snapshot.release` only releases an artifact and takes no document target.

Omitting `target` retains focus-based compatibility. The Python MCP tools expose
the pair as optional `instance_id` and `document_id` arguments; both must be
present or both absent. Explicit targets never use CLI fallback. Mutations are
never replayed automatically after a lost response.
