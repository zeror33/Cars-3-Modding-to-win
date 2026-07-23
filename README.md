# Cars 3 Driven to Win - Web Model Viewer

A web-based 3D model viewer and file explorer designed for inspecting assets, textures, and geometry formats from *Cars 3: Driven to Win*.

---

## Features

* **3D Model Viewer**
  * Built with **Three.js** and WebGL.
  * Supports wireframe toggle and material visualization.

* **Texture Decoders**
  * Decodes texture formats including BC1–BC7 and RGBA8.
  * Applies MATP material maps directly to models.

* **ROMFS Browser**
  * Interactive file browser for browsing extracting game data.
  * Integrated search to find specific assets easily.

---

## Supported File Formats

| File Extension | Description |
| :--- | :--- |
| `.oct` | Mesh container / Scene data |
| `.vbuf` | Vertex Buffer data |
| `.ibuf` | Index Buffer data |
| `.mtb` | Material Binary definitions |
| `.bct` | Binary Texture data |

---

## Directory Layout

```text
├── romfs/                  # Place your extracted game assets here
├── static/                 # Frontend assets and Three.js dependencies
│   ├── js/
│   └── css/
├── templates/              # HTML templates for the web interface
├── cars3_viewer.py         # Main application entry point
└── README.md
Quick Start
Clone the repository:

Bash
git clone [https://github.com/your-username/your-repo-name.git](https://github.com/your-username/your-repo-name.git)
cd your-repo-name
Install dependencies:

Bash
pip install -r requirements.txt
Run the viewer:

Bash
python cars3_viewer.py
Access the web interface:

Open your browser and navigate to http://localhost:8766

Credits & Acknowledgments
cars3-blender-io by DJmax0955 — Format reverse-engineering and research.

RevOctane by zzh8829 — Octane engine research and tools.

License
This project is open-source and intended for educational and research purposes.
