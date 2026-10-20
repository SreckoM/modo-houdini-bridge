'''
Modo -> Houdini bridge -- USD writer

No Modo imports in here: the reader in houdini_bridge_cmds.py turns the Modo
scene into plain Python data, and this module writes that data as USD. Inside
Modo it uses Modo's own USD bindings (fnpxr); in hython (tests) it uses pxr.

Scene data
----------
    {
      'items': [ItemData, ...],          # parents before children
      'materials': {tag: MaterialData},  # every material tag used
    }

    ItemData = {
      'id':      unique Modo item id,
      'parent':  id of the parent item or None,
      'name':    Modo item name,
      'matrix':  world matrix, 4 rows, row vectors (translation in last row),
      'hidden':  bool,
      'mesh':    MeshData or None,
    }

    MeshData = {
      'points':  [(x, y, z), ...]                  local space, metres
      'faces':   [[point index, ...], ...]         Modo vertex order (CCW)
      'subd':    [bool per face]                   SubD or PSub
      'material':[tag per face]
      'part':    [tag per face]                    '' when untagged
      'sets':    [[set name, ...] per face]
      'uvs':     [(map name, [(u, v) per face vertex]), ...]
      'normals': [(x, y, z) per face vertex] or None
      'colors':  [(map name, size, [tuple per face vertex]), ...]
      'weights': [(map name, [float per point]), ...]
      'creases': [((point a, point b), modo edge weight), ...]
      'corners': [(point, modo vertex weight), ...]
    }

    MaterialData = {'color': (r, g, b), 'roughness': f, 'metallic': f,
                    'opacity': f}

Layout
------
    /modo                        Xform, default prim
      /<Item>/<Child>...         Xform per Modo item, local transform
        /<Item>                  Mesh (SubD faces if the item has any)
        /<Item>_faces            Mesh, only when SubD and faces are mixed
          mat_<tag>              GeomSubset, family materialBind
          part_<tag>             GeomSubset, family part
          set_<name>             GeomSubset, family selectionSet
    /materials/<tag>             stand-in UsdPreviewSurface material

Prim paths only depend on item names and tags, so Houdini edits that target
them survive the next send.
'''

import os
import re

try:
    from fnpxr import Gf, Sdf, Usd, UsdGeom, UsdShade, Vt
except ImportError:  # hython, tests
    from pxr import Gf, Sdf, Usd, UsdGeom, UsdShade, Vt


ROOT = '/modo'
MATERIALS = '/materials'
DEFAULT_TAG = 'Default'

# Modo edge/vertex weights run 0..1 (100%); USD sharpness 10 is infinitely
# sharp. For PSub this is exact: frozen Modo 17.1 PSub cubes matched
# OpenSubdiv in Houdini to 1e-7 for edge weights 0.05-0.25 (sharpness
# 0.5-2.5) and a 10% corner weight (sharpness 1). Modo's own SubD is a
# different scheme, so SubD meshes only come close.
SHARPNESS_SCALE = 10.0


class ExportError(Exception):
    pass


# ---------------------------------------------------------------------------
# names
# ---------------------------------------------------------------------------
def prim_name(name, fallback='item'):
    '''A valid USD prim name for an arbitrary Modo name.'''
    clean = re.sub(r'[^A-Za-z0-9_]', '_', name or '').strip('_') or fallback
    clean = re.sub(r'_+', '_', clean)
    if clean[0].isdigit():
        clean = '_' + clean
    return clean


class NameTable(object):
    '''Hands out unique prim names among siblings, first come first served.'''

    def __init__(self, reserved=()):
        self._used = set(reserved)

    def take(self, name, fallback='item'):
        base = prim_name(name, fallback)
        candidate, n = base, 1
        while candidate in self._used:
            candidate = '%s_%d' % (base, n)
            n += 1
        self._used.add(candidate)
        return candidate


# ---------------------------------------------------------------------------
# mesh helpers
# ---------------------------------------------------------------------------
def split_mesh(mesh):
    '''[(suffix, MeshData)]: one mesh, or SubD and plain faces separately
    when the Modo mesh mixes them (USD sets the scheme per mesh).'''
    faces = mesh['faces']
    subd = mesh['subd']
    if not faces:
        return []
    if all(subd) or not any(subd):
        return [('', mesh)]
    return [('', _subset_mesh(mesh, [i for i, s in enumerate(subd) if s])),
            ('_faces', _subset_mesh(mesh, [i for i, s in enumerate(subd)
                                           if not s]))]


def _subset_mesh(mesh, keep):
    '''The faces in `keep`, with points renumbered to the ones they use.'''
    remap = {}
    for f in keep:
        for p in mesh['faces'][f]:
            if p not in remap:
                remap[p] = len(remap)
    order = sorted(remap, key=remap.get)

    # face vertex offsets in the source mesh
    starts, total = [], 0
    for face in mesh['faces']:
        starts.append(total)
        total += len(face)

    def fv(values):
        out = []
        for f in keep:
            out.extend(values[starts[f]:starts[f] + len(mesh['faces'][f])])
        return out

    out = {
        'points': [mesh['points'][p] for p in order],
        'faces': [[remap[p] for p in mesh['faces'][f]] for f in keep],
        'subd': [mesh['subd'][f] for f in keep],
        'material': [mesh['material'][f] for f in keep],
        'part': [mesh['part'][f] for f in keep],
        'sets': [mesh['sets'][f] for f in keep],
        'uvs': [(name, fv(vals)) for name, vals in mesh.get('uvs', [])],
        'normals': fv(mesh['normals']) if mesh.get('normals') else None,
        'colors': [(name, size, fv(vals))
                   for name, size, vals in mesh.get('colors', [])],
        'weights': [(name, [vals[p] for p in order])
                    for name, vals in mesh.get('weights', [])],
        'creases': [((remap[a], remap[b]), w)
                    for (a, b), w in mesh.get('creases', [])
                    if a in remap and b in remap],
        'corners': [(remap[p], w) for p, w in mesh.get('corners', [])
                    if p in remap],
    }
    return out


def indexed(values):
    '''(unique values, index per element) -- for indexed primvars. Exact
    matches only: Modo hands out identical floats for shared values.'''
    lookup = {}
    idx = [lookup.setdefault(v, len(lookup)) for v in map(tuple, values)]
    return list(lookup), idx


# ---------------------------------------------------------------------------
# writing
# ---------------------------------------------------------------------------
def write_stage(path, scene, ascii=False):
    '''Write `scene` to `path` (.usdc or .usda) through a temporary file so a
    reader never sees half a file. Returns a stats dict.'''
    path = os.path.abspath(path)
    folder = os.path.dirname(path)
    if folder and not os.path.isdir(folder):
        os.makedirs(folder)
    ext = '.usda' if ascii else '.usdc'
    tmp = path + '.tmp' + ext
    layer = Sdf.Layer.CreateAnonymous(ext)
    stage = Usd.Stage.Open(layer)
    stats = build(stage, scene)
    layer.Export(tmp)
    os.replace(tmp, path)
    return stats


def build(stage, scene):
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.y)
    UsdGeom.SetStageMetersPerUnit(stage, 1.0)
    root = UsdGeom.Xform.Define(stage, ROOT)
    stage.SetDefaultPrim(root.GetPrim())

    stats = {'meshes': 0, 'faces': 0, 'items': 0, 'materials': 0}
    mat_paths = _write_materials(stage, scene.get('materials', {}))
    stats['materials'] = len(mat_paths)

    paths = {}          # item id -> Sdf.Path
    worlds = {}         # item id -> Gf.Matrix4d
    names = {None: NameTable()}
    for item in scene['items']:
        parent = item.get('parent')
        if parent not in paths:
            parent = None
        parent_path = paths[parent] if parent is not None else Sdf.Path(ROOT)
        name = names[parent].take(item['name'])
        names[item['id']] = NameTable()
        path = parent_path.AppendChild(name)
        paths[item['id']] = path

        world = Gf.Matrix4d(*[float(v) for r in item['matrix'] for v in r])
        worlds[item['id']] = world
        local = world
        if parent is not None:
            local = world * worlds[parent].GetInverse()
        xf = UsdGeom.Xform.Define(stage, path)
        if local != Gf.Matrix4d(1.0):
            xf.AddTransformOp().Set(local)
        if item.get('hidden'):
            xf.CreateVisibilityAttr(UsdGeom.Tokens.invisible)
        stats['items'] += 1

        mesh = item.get('mesh')
        if not mesh:
            continue
        for suffix, part in split_mesh(mesh):
            child = names[item['id']].take(name + suffix)
            _write_mesh(stage, path.AppendChild(child), part, mat_paths)
            stats['meshes'] += 1
            stats['faces'] += len(part['faces'])
    return stats


def _write_materials(stage, materials):
    UsdGeom.Scope.Define(stage, MATERIALS)
    table = NameTable()
    out = {}
    for tag in sorted(materials):
        data = materials[tag] or {}
        path = Sdf.Path(MATERIALS).AppendChild(table.take(tag, 'material'))
        mat = UsdShade.Material.Define(stage, path)
        mat.GetPrim().SetCustomDataByKey('modo:tag', tag)
        shader = UsdShade.Shader.Define(stage, path.AppendChild('preview'))
        shader.CreateIdAttr('UsdPreviewSurface')
        shader.CreateInput('diffuseColor', Sdf.ValueTypeNames.Color3f).Set(
            Gf.Vec3f(*data.get('color', (0.6, 0.6, 0.6))))
        shader.CreateInput('roughness', Sdf.ValueTypeNames.Float).Set(
            float(data.get('roughness', 0.5)))
        shader.CreateInput('metallic', Sdf.ValueTypeNames.Float).Set(
            float(data.get('metallic', 0.0)))
        shader.CreateInput('opacity', Sdf.ValueTypeNames.Float).Set(
            float(data.get('opacity', 1.0)))
        mat.CreateSurfaceOutput().ConnectToSource(
            shader.ConnectableAPI(), 'surface')
        out[tag] = (path, data.get('color', (0.6, 0.6, 0.6)))
    return out


def _write_mesh(stage, path, mesh, mat_paths):
    usd = UsdGeom.Mesh.Define(stage, path)
    prim = usd.GetPrim()
    faces = mesh['faces']

    usd.CreatePointsAttr(Vt.Vec3fArray(mesh['points']))
    usd.CreateFaceVertexCountsAttr(Vt.IntArray([len(f) for f in faces]))
    usd.CreateFaceVertexIndicesAttr(Vt.IntArray([i for f in faces for i in f]))
    usd.CreateOrientationAttr(UsdGeom.Tokens.rightHanded)
    usd.CreateExtentAttr(UsdGeom.Boundable.ComputeExtentFromPlugins(
        usd, Usd.TimeCode.Default()))

    subd = all(mesh['subd'])
    usd.CreateSubdivisionSchemeAttr(
        UsdGeom.Tokens.catmullClark if subd else UsdGeom.Tokens.none)

    pv = UsdGeom.PrimvarsAPI(prim)
    T = Sdf.ValueTypeNames

    # UVs: the first map becomes st, the way Houdini and Karma expect
    uv_names = NameTable(('st', 'normals', 'displayColor'))
    for n, (name, values) in enumerate(mesh.get('uvs', [])):
        pname = 'st' if n == 0 else uv_names.take(name, 'uv')
        uniq, idx = indexed(values)
        var = pv.CreatePrimvar(pname, T.TexCoord2fArray,
                               UsdGeom.Tokens.faceVarying)
        var.Set(Vt.Vec2fArray(uniq))
        var.SetIndices(Vt.IntArray(idx))
        var.GetAttr().SetCustomDataByKey('modo:map', name)

    if mesh.get('normals') and not subd:
        uniq, idx = indexed(mesh["normals"])
        var = pv.CreatePrimvar('normals', T.Normal3fArray,
                               UsdGeom.Tokens.faceVarying)
        var.Set(Vt.Vec3fArray(uniq))
        var.SetIndices(Vt.IntArray(idx))

    for name, size, values in mesh.get('colors', []):
        uniq, idx = indexed(values)
        if size == 4:
            var = pv.CreatePrimvar(uv_names.take(name, 'color'),
                                   T.Color4fArray, UsdGeom.Tokens.faceVarying)
            var.Set(Vt.Vec4fArray([Gf.Vec4f(*v) for v in uniq]))
        else:
            var = pv.CreatePrimvar(uv_names.take(name, 'color'),
                                   T.Color3fArray, UsdGeom.Tokens.faceVarying)
            var.Set(Vt.Vec3fArray([Gf.Vec3f(*v[:3]) for v in uniq]))
        var.SetIndices(Vt.IntArray(idx))
        var.GetAttr().SetCustomDataByKey('modo:map', name)

    for name, values in mesh.get('weights', []):
        var = pv.CreatePrimvar(uv_names.take(name, 'weight'), T.FloatArray,
                               UsdGeom.Tokens.vertex)
        var.Set(Vt.FloatArray([float(v) for v in values]))
        var.GetAttr().SetCustomDataByKey('modo:map', name)

    if subd:
        creases = [(e, w) for e, w in mesh.get('creases', []) if w > 0]
        if creases:
            usd.CreateCreaseIndicesAttr(
                Vt.IntArray([i for (a, b), _ in creases for i in (a, b)]))
            usd.CreateCreaseLengthsAttr(Vt.IntArray([2] * len(creases)))
            usd.CreateCreaseSharpnessesAttr(Vt.FloatArray(
                [min(w, 1.0) * SHARPNESS_SCALE for _, w in creases]))
        corners = [(p, w) for p, w in mesh.get('corners', []) if w > 0]
        if corners:
            usd.CreateCornerIndicesAttr(Vt.IntArray([p for p, _ in corners]))
            usd.CreateCornerSharpnessesAttr(Vt.FloatArray(
                [min(w, 1.0) * SHARPNESS_SCALE for _, w in corners]))

    # tags as per face strings -- the exact Modo names
    mats = [t or DEFAULT_TAG for t in mesh['material']]
    pv.CreatePrimvar('modo_material', T.StringArray,
                     UsdGeom.Tokens.uniform).Set(Vt.StringArray(mats))
    if any(mesh['part']):
        pv.CreatePrimvar('modo_part', T.StringArray,
                         UsdGeom.Tokens.uniform).Set(
                             Vt.StringArray(mesh['part']))
    colors = [mat_paths.get(t, (None, (0.6, 0.6, 0.6)))[1] for t in mats]
    uniq, idx = indexed(colors)
    var = pv.CreatePrimvar('displayColor', T.Color3fArray,
                           UsdGeom.Tokens.uniform)
    var.Set(Vt.Vec3fArray([Gf.Vec3f(*c) for c in uniq]))
    var.SetIndices(Vt.IntArray(idx))

    # ... and as face subsets
    subsets = NameTable()
    binding = UsdShade.MaterialBindingAPI.Apply(prim)
    by_tag = _faces_by(mats)
    for tag in sorted(by_tag):
        sub = UsdGeom.Subset.Define(
            stage, path.AppendChild(subsets.take('mat_' + prim_name(tag))))
        _fill_subset(sub, by_tag[tag], 'materialBind', tag)
        if tag in mat_paths:
            UsdShade.MaterialBindingAPI.Apply(sub.GetPrim()).Bind(
                UsdShade.Material(stage.GetPrimAtPath(mat_paths[tag][0])))
    if by_tag:
        binding.SetMaterialBindSubsetsFamilyType(UsdGeom.Tokens.partition)

    by_part = _faces_by(mesh['part'])
    for tag in sorted(t for t in by_part if t):
        sub = UsdGeom.Subset.Define(
            stage, path.AppendChild(subsets.take('part_' + prim_name(tag))))
        _fill_subset(sub, by_part[tag], 'part', tag)
    if any(by_part):
        UsdGeom.Subset.SetFamilyType(UsdGeom.Imageable(prim), 'part',
                                     UsdGeom.Tokens.nonOverlapping)

    by_set = {}
    for f, names in enumerate(mesh['sets']):
        for name in names:
            by_set.setdefault(name, []).append(f)
    for name in sorted(by_set):
        sub = UsdGeom.Subset.Define(
            stage, path.AppendChild(subsets.take('set_' + prim_name(name))))
        _fill_subset(sub, by_set[name], 'selectionSet', name)
    if by_set:
        UsdGeom.Subset.SetFamilyType(UsdGeom.Imageable(prim), 'selectionSet',
                                     UsdGeom.Tokens.unrestricted)
    return usd


def _faces_by(tags):
    out = {}
    for f, tag in enumerate(tags):
        out.setdefault(tag, []).append(f)
    return out


def _fill_subset(sub, faces, family, tag):
    sub.CreateElementTypeAttr(UsdGeom.Tokens.face)
    sub.CreateIndicesAttr(Vt.IntArray(faces))
    sub.CreateFamilyNameAttr(family)
    sub.GetPrim().SetCustomDataByKey('modo:tag', tag)
