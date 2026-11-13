# System overview (PDF)

`lft3_system_overview.pdf`: how the whole system works (board, packets, host, real-time runtime, products,
viewer, search, performance, design decisions), with diagrams.

Regenerate after changes (needs reportlab + matplotlib + pillow, e.g. in a scratch venv):

```bash
cd docs/system_overview
python figures.py figures      # diagrams and charts → figures/*.png
python build.py                # → lft3_system_overview.pdf
```

Numbers in the text and figures are the measurements recorded in `docs/runtime.md`; update both together.
