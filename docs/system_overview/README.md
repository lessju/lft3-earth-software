# System overview (PDF)

`lft3_system_overview.pdf`: how the whole system works (board, packets, host, real-time runtime, products,
viewer, search, performance, design decisions), with diagrams.

Regenerate after changes (needs reportlab, svglib and matplotlib, e.g. in a scratch venv):

```bash
cd docs/system_overview
python figures.py figures      # diagrams and charts → figures/*.svg (vector, text kept as text)
python build.py                # → lft3_system_overview.pdf
```

Numbers in the text and figures are the measurements recorded in `docs/runtime.md` and `docs/sizing.md`; the
sizing charts are drawn from `docs/sizing_figures/sizing_model.py`.
The resolution chart reads `data/resolution_bench.json` (copied from `pipeline/bench/resolution` results). Keep file paths in the appendix only.
