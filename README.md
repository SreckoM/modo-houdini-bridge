# Modo → Houdini bridge

Model in Modo, light and render in Houdini. **Send to Houdini** writes the
meshes to one USD file and tells Houdini to reload it. Anything you add in
Houdini after the Modo layer stays when you send again.

**Early test version (0.1).** Only meshes and their material tags, parts and
selection sets are sent so far. Tested on Linux: Modo 16.1v9 and 17 →
Houdini 21.0 and 22.0. Windows and macOS should work (plain Python, no
compiled parts) but are untested. Houdini 20.5 is untested.

## Install

Pure Python, nothing to compile. Modo 16.1+ and Houdini 21+.

1. **Modo:** clone or unzip this repo into your Modo Kits folder, e.g. as
   `houdini_bridge`:

   | OS | Kits folder |
   |---|---|
   | Linux | `~/.luxology/Kits` |
   | Windows | `%APPDATA%\Luxology\Kits` |
   | macOS | `~/Library/Application Support/Luxology/Kits` |

   ```
   cd <Kits folder>
   git clone https://github.com/SreckoM/modo-houdini-bridge houdini_bridge
   ```
   Restart Modo. A **Houdini** button appears in the mode bar, top right.
2. **Houdini:** run the installer once per Houdini version, with that
   version's hython:
   ```
   <houdini install>/bin/hython <Kits folder>/houdini_bridge/houdini/install_package.py
   ```
   It writes `modo_bridge.json` into your `houdiniXX.X/packages` folder. You
   can also run the file from Houdini's Python Shell with
   `exec(open(r"<path>/install_package.py").read())`. Restart Houdini, then
   add the **Modo Bridge** shelf (shelf set "+" → Shelves → Modo Bridge).

## Use

1. In Houdini, click **Modo Stage** to add a Sublayer LOP in `/stage`, or
   **Modo Geometry** to add polygons in SOPs, or both.
2. In Modo, click **Houdini → Send to Houdini**. Houdini reloads by itself.
   If Houdini isn't listening, press **Reload Modo** there.

Settings, in the Houdini popover in Modo:

| Setting | Default | |
|---|---|---|
| Items | All meshes | or the selected items and their children. The file holds only what was sent last. |
| File | `~/modo_bridge/scene.usdc` | `.usda` writes text, which is useful for checking |
| Port | 51234 | on the Houdini side: `MODO_BRIDGE_PORT` |

Houdini listens automatically in graphical sessions. Set
`MODO_BRIDGE_AUTOSTART=0` to turn that off. Only one Houdini session can
listen at a time; **Listen to Modo** toggles it.

## What arrives where

```
/modo                        default prim, metres, Y up
  /Group_A                   Xform per Modo item (locators, groups, meshes)
    /Box                     Xform with the item's local transform
      /Box                   Mesh: SubD/PSub -> catmullClark, faces -> none
      /Box_faces             only when a mesh mixes SubD and plain faces
        mat_Red_Paint        GeomSubset, family materialBind -> /materials/Red_Paint
        part_Lid             GeomSubset, family part
        set_Set_A            GeomSubset, family selectionSet (may overlap)
/materials/Red_Paint         stand-in UsdPreviewSurface (Modo diffuse colour,
                             roughness, metallic, opacity)
```

Names are cleaned up for USD (`Red Paint` → `Red_Paint`, duplicates get
`_1`). The exact Modo names are kept in the `modo_material` and `modo_part`
attributes, and in each subset's and material's `modo:tag` custom data.

| Modo | USD primvar | Houdini SOPs (Modo Geometry) |
|---|---|---|
| material tag | `modo_material`, subsets `mat_*` | groups `mat_*`, attributes `modo_material`, `materialBind`, `usdmaterialpath` |
| part | `modo_part`, subsets `part_*` | groups `part_*`, attributes `modo_part`, `part` |
| selection set (polygons) | subsets `set_*` | groups `set_*` |
| first UV map / others | `st` / map name | `uv` / map name |
| vertex normals | `normals` (plain faces only) | `N` |
| RGB / RGBA maps | faceVarying colours | vertex attributes |
| weight maps | vertex floats | point attributes |
| SubD edge / vertex weights | creases / corners | `creaseweight` |

* **Materials in Solaris:** put your real material at the same path
  (`/materials/Red_Paint`, for example with a Material Library LOP after the
  Modo Stage node). It takes over from the stand-in. Paths only change when
  you rename items or tags in Modo.
* **Normals** come from Modo, so they follow each material's smoothing
  angle, and a vertex normal map wins when there is one. SubD meshes get none;
  the renderer subdivides them.
* **SubD vs PSub:** USD and Houdini subdivide with OpenSubdiv
  Catmull-Clark, which is Modo's **PSub**. PSub meshes, including their edge
  and vertex weights (weight × 10 = sharpness, 100% = infinitely sharp),
  come out identical. Modo's own **SubD** is a different scheme. The
  same cage shrinks less in Houdini (a unit cube at level 2: corners at
  0.255 vs 0.236 in Modo), and edge weights below 100% are much softer in
  Modo (50% there is closer to sharpness 0–1 than to 5). Convert to PSub
  (Shift+Tab) for an exact match. The Event Log says when a send contains
  SubD polygons.
* **Hidden items** (eye off) are sent as invisible.
* Meshes are sent evaluated at the current frame, with mesh operations and
  deformers applied.
* Polygons with the part `Default` are treated as having no part, because
  Modo assigns that name itself.

## Not yet

* Modo SubD (not PSub) shapes are only approximated, see above. Boundary
  and UV interpolation rules are not mapped; USD defaults are used.
* Not sent yet: mesh instances, replicators, static meshes, curves, cameras,
  lights, animation, morphs, textures, vertex/edge selection sets.
* Speed: about 2.5 s to read and 1 s to write 131k faces (Modo 16). Reading
  is plain Python.

## Help testing

Please open an issue with:

* OS, Modo version, Houdini version
* what you sent (a small `.lxo` is ideal) and what looked wrong in Houdini
* the Modo Event Log line starting with `Houdini bridge:`, and any
  `Modo Bridge:` lines or errors in Houdini's console

Things to look at in particular: open boundaries and UV seams on PSub
meshes, hard-surface smoothing compared with Modo, heavy meshes, and
Windows/macOS installs.

## Tests

```
/opt/hfs22.0/bin/hython tests/test_usd.py   # writer + Houdini nodes, no Modo
tests/run_e2e.sh                            # headless Modo 16 + 17 -> every hython
```

`tests/modo_fixture.py` notes a few Modo API quirks it works around: no
polygon selection in `modo_cl`, one tag type per edit, and map values lost
when written in the edit that creates the map.
