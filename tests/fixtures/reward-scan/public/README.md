# Required gameplay reward frames

Four gameplay screenshots used by the reward OCR tests. Black rectangles cover
squad names and account icons; pixels outside those rectangles are unchanged.
PNG metadata was removed. `manifest.json` records dimensions, expected slot labels,
mask rectangles and SHA-256 hashes. Capture dates, game versions and interface
scale settings are unknown.

- `real-full-2p.png`: wrapped Khora title beside Fang, with an open item tooltip.
- `real-full-4p-fps.png`: four different items, wrapped Xaku title and FPS noise.
- `real-full-4p-1080x607.png`: four different items with small text.
- `real-full-4p-16x10.png`: 1899x1199, four identical quantity-prefixed Forma slots.

The identical Forma slots test recognition at this aspect ratio but cannot detect
swapped slots. These fixtures run through production OCR and matching; live capture
and event timing require separate tests. Additional private screenshots are optional.
