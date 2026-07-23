Cars 3: Driven to Win — ROMFS Explorer & Model Viewer

An interactive web-based explorer and 3D viewer toolset built in Python and Three.js to browse, inspect, and render asset containers from Cars 3: Driven to Win (.octane, .oct, .vbuf, .ibuf, .mtb, .tbody).

 Key Features

🚘 Interactive 3D Model Viewer

WebGL Rendering: Powered by Three.js with full PBR shading, ambient/directional lighting, and grid guides.

Camera & Orbit Controls: Smooth mouse controls for orbiting, panning, and zooming, with camera reset and auto-spin controls.

Wireframe Mode: Toggle wireframe overlays across loaded mesh primitives.

Geometry Decoding: Full extraction of vertex pools (.vbuf) and index pools (.ibuf) using dynamic stride, offset, float16 UV, and unit-scale transformations.

Material & Texture Mapping: Automatic parsing of MATP material tables to map texture slots directly to character and environmental sub-meshes.

🎨 Texture Decompression Engine

Texture Formats Supported:

BC1 / DXT1 (RGB 8-byte compressed)

BC3 / DXT5 (RGBA 16-byte compressed with alpha table)

BC4 (Single channel / grayscale)

BC5 (Two-channel compression)

BC7 (Advanced high-quality compressed textures)

RGBA8 (Uncompressed raw color channels)

Heuristic Dimension Resolver: Computes power-of-2 aspect ratios directly from compressed block counts without relying on external metadata headers.

📁 Full ROMFS File Browser & Inspection

Directory Traversal: Safely explore game assets (romfs/assets/) including characters, environment models, choreographies, weapons, objects, and realms.

ZIP Container Inspector: Inspect nested .zip archives directly from the browser UI and extract individual internal streams.

Multi-Format Content Previewer:

Text & Code: .txt, .lua, .json, .xml, .cfg, .csv, .yaml, .py

Images: .png, .jpg, .bmp

Audio & Video: .wem (Wwise audio previewing), .bnk, .pck, and .bik metadata summary

Binary Inspection: Integrated hex viewer for raw .bin, .vbuf, .ibuf, .bent, .bct, and .banm files.

Global Search: Fast full-text search across all file names within the romfs/ tree.

🏗 Directory Structure

To run the explorer, ensure your romfs dump is placed alongside the scripts:

.
├── cars3_viewer.py          # Python server backend & file parser
├── viewer.html              # Frontend UI layout & styling
├── three.min.js             # Three.js WebGL engine
├── bc7_decoder.js           # BC7 texture decoder library
├── RevOctane/               # Python core stream parser module
│   ├── bstream.py
│   └── revoctane.py
└── romfs/                   # Extracted game filesystem
    └── assets/
        ├── characters/      # Character ZIP archives (e.g. McQueen, Cruz)
        ├── env_assets/      # Environment models
        ├── objects/         # Props & world items
        └── ...


🚀 Quick Start

1. Requirements

Python 3.8+

Modern web browser (Chrome, Firefox, Edge, Safari) with WebGL enabled.

No external Python dependencies required (uses built-in http.server, zipfile, struct, and json libraries).

2. Launching the Server

Run the local HTTP server script from the project root:

python3 cars3_viewer.py


3. Accessing the Viewer

Open your web browser and navigate to:

http://127.0.0.1:8766


🐍 Python API Usage

You can also use the core backend modules independently to programmatically extract section data or metadata:

import bstream
from revoctane import Octane

# Open and parse an OCT container stream
with open("romfs/assets/characters/cars3_mcqueen/model.oct", "rb") as f:
    stream = bstream.BStream(f.read())
    octane = Octane(stream)

# Inspect section pools
vpool = octane.get("VertexBufferPool", {})
ipool = octane.get("IndexBufferPool", {})
matpool = octane.get("MaterialPool", {})

print(f"Loaded {len(vpool)} vertex buffers and {len(matpool)} materials.")


🤝 Credits & Acknowledgments

RevOctane by zzh8829 — Core reverse-engineering parser module and binary stream reader for Octane asset containers.

cars3-blender-io by DJmax0955 — Invaluable reverse-engineering research and format documentation for Cars 3: Driven to Win binary containers (.oct, .vbuf, .ibuf, .mtb).

Three.js — WebGL 3D rendering library.
