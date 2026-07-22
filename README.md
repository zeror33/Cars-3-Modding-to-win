# Cars 3: Driven to Win — Ultimate Live Editor

A web-based live editor and asset explorer for *Cars 3: Driven to Win* (3DS / Switch / Wii U / Console assets).

> ⚠️ **Status: Work In Progress / Experimental**
> - **File Browsing & Searching:** Functional
> - **Texture Decoding (BC1 / BC3 DXT):** Functional
> - **3D Model Mesh Extraction:** Partial / In Development *(Models currently import with separated primitives/alignment offsets that need vertex dequantization tuning)*.

---

## 🏎️ Overview

This tool provides a local web interface to inspect, parse, and edit bundled game assets from *Cars 3: Driven to Win*. It includes custom parsers for Avalanche Engine Octane formats (`.oct`, `.bent`), texture decoding capabilities, and a integrated Three.js 3D viewport.

---

## ✨ Features

- **Asset Tree & File Browser:** Browse unpacked `RomFS` and `ExeFS` game directories.
- **Search & Filter:** Search for character bundles, textures, and mesh files across the asset tree.
- **Texture Preview:** Auto-decodes compressed texture formats (BC1/BC3 / DXT1/DXT5) into standard browser viewable images.
- **3D Model Viewer (Three.js):** - Interactive camera controls (orbit, pan, zoom).
  - Wireframe toggle and lighting presets.
  - Sub-mesh / Primitive structural breakdown.

---

## 🚧 Current Issues & Known Limitations

- **3D Model Sub-mesh Alignment (WIP):** Character models currently extract into separate disconnected primitives (body shell, chassis, spoiler, 4 wheels). Vertex positions are dequantized from `uint16` values; fine-tuning relative transform offsets (`transX`, `transY`, `transZ`) and master bounding box scales (`global_aabb`) is actively being worked on.
- **Texture Mapping:** Multi-material texture mapping onto sub-primitives is undergoing refinement.

---

## 🛠️ Project Structure

```text
├── cars3_app.py        # Python backend (HTTP API, Octane parsing, Texture decoding)
├── index.html          # Frontend single-page application (UI, TreeView, Three.js Viewport)
├── RevOctane/          # (Optional) Binary stream / Octane file parsing helper library
├── romfs/              # Extracted RomFS game directory
└── exefs/              # Extracted ExeFS game directory

```

---

## 🚀 Getting Started

### Prerequisites

* **Python 3.8+**
* Recommended Python packages:
```bash
pip install pillow

```



### Running the Editor

1. Place your extracted `romfs` and `exefs` folders in the root project directory alongside `cars3_app.py`.
2. Start the local server:
```bash
python cars3_app.py

```


3. Open your browser and navigate to:
```text
[http://127.0.0.1:8765](http://127.0.0.1:8765)

```



---

## 🤝 Contributing

Contributions to fix vertex dequantization or contribute format specifications for Avalanche Octane assets are welcome! Feel free to open an issue or submit a pull request.
