# Saved UI themes

Snapshots of full `frontend/index.html` at a given visual style, so a look can
be restored later by copying the snapshot back over `frontend/index.html`
(or lifting its `<style>` block + icon set).

- **paper-ledger.snapshot.html** — the skeuomorphic "paper & cardboard" look:
  ledger-green paper manifest (left list, alternating ruled rows), warm orange
  "info card" inspect panel, colored-in-pencil forecast dots & chart bars,
  round pencil-textured step buttons, monochrome line-icon set, Inter type.
  To restore: `cp themes/paper-ledger.snapshot.html index.html`.
