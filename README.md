# Cars 3: Driven to Win — Ultimate Live Editor

A web-based live editor and asset explorer for Cars 3: Driven to Win (3DS / Switch / Wii U / Console assets).

> **Status: Work In Progress / Experimental**
> - **File Browsing & Searching:** Functional
> - **Texture Decoding (BC1 / BC3 DXT):** Functional
> - **3D Model Mesh Extraction:** Partial / In Development (Vertex dequantization and sub-mesh primitive assembly are currently being refined).

---

## Overview

This tool provides a local web interface to inspect, parse, and edit bundled game assets from Cars 3: Driven to Win. It includes custom parsers for Avalanche Software's Octane engine formats (.oct, .bent), texture decoding capabilities, and an integrated Three.js 3D viewport.

---

## Credits & Acknowledgments

This project relies on the reverse-engineering work and Octane file format specifications developed by:

- **[DJmax0955/cars3-blender-io](https://github.com/DJmax0955/cars3-blender-io/tree/main)** — For reverse engineering Avalanche Octane/BENT character models, vertex quantization structures, and primitive node hierarchies.

---

## Features

- **Asset Tree & File Browser:** Browse unpacked RomFS and ExeFS game directories.
- **Search & Filter:** Search for character bundles, textures, and mesh files across the asset tree.
- **Texture Preview:** Auto-decodes compressed texture formats (BC1/BC3 / DXT1/DXT5) into standard browser-viewable images.
- **3D Model Viewer (Three.js):**
  - Interactive camera controls (orbit, pan, zoom).
  - Wireframe toggle and lighting presets.
  - Primitive / sub-mesh structural breakdown.

---

## Current Issues & Known Limitations

- **3D Model Sub-mesh Assembly (WIP):** Character models extract into separate primitives (body shell, chassis, spoiler, faceplate, wheels). Work is ongoing to correctly apply master bounding box (global_aabb) dequantization and node transform vectors (transX, transY, transZ) to render the car fully assembled.

---

## Project Structure

```text
├── cars3_app.py        # Python backend (HTTP API, Octane parsing, Texture decoding)
├── index.html          # Frontend single-page application (UI, TreeView, Three.js Viewport)
├── RevOctane/          # Binary stream / Octane file parsing helper library
├── romfs/              # Extracted RomFS game directory
└── exefs/              # Extracted ExeFS game directory

```

---

## Getting Started

### Prerequisites

* **Python 3.8+**
* Pillow library (optional, for enhanced texture rendering):
```bash
pip install pillow

```



### Running the Editor

1. Place your extracted `romfs` and `exefs` folders in the root project directory alongside `cars3_app.py`.
2. Start the server:
```bash
python cars3_app.py

```


3. Open your web browser to:
```text
[http://127.0.0.1:8765](http://127.0.0.1:8765)

```



---

## Contributing

Contributions to improve Octane vertex parsing or UI capabilities are welcome! Feel free to open an issue or submit a pull request.

```

```
