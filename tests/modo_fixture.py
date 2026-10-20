#python
'''
Fixture for the end-to-end test (run_e2e.sh): builds a scene in a headless
Modo, then runs houdini.send. The output path comes from the environment
variable HB_OUT.

  Group A (locator, moved +2 Y)
    Box     cube, all SubD; tags: Red Paint/Lid/Set A on faces 0-1,
            Set B on face 1, Body on the rest; edge 0 weighted 50%;
            weight map "Weight" with point 0 = 1
  Ball      sphere, plain faces, hidden; no tags (-> Default)
  Mixed     cube, face 0 SubD, the rest plain faces; tag "Steel"
'''

import os

import lx
import modo


def mesh_named(name):
    for item in modo.Scene().items('mesh'):
        if item.name == name:
            return item
    raise RuntimeError(name)


def new_prim(tool, name, extra=()):
    lx.eval('item.create mesh "%s"' % name)
    item = modo.Scene().selected[0]
    lx.eval('tool.set %s on' % tool)
    lx.eval('tool.reset')
    for attr in extra:
        lx.eval('tool.attr %s %s' % (tool, attr))
    lx.eval('tool.doApply')
    lx.eval('tool.set %s off' % tool)
    return item


def edit(item, fn):
    '''Run fn(mesh) on an editable mesh of `item`.'''
    modo.Scene().select(item)
    scan = lx.service.Layer().ScanAllocate(
        lx.symbol.f_LAYERSCAN_EDIT | lx.symbol.f_LAYERSCAN_PRIMARY)
    mesh = lx.object.Mesh(scan.MeshEdit(0))
    fn(mesh)
    scan.SetMeshChange(0, lx.symbol.f_MESHEDIT_POLYGONS
                       | lx.symbol.f_MESHEDIT_POL_TAGS
                       | lx.symbol.f_MESHEDIT_MAP_OTHER
                       | lx.symbol.f_MESHEDIT_MAP_CONTINUITY)
    scan.Apply()


def _tag_pass(mesh, kind, value_for):
    # editing a polygon can renumber the others: walk by ID, not index
    pol = mesh.PolygonAccessor()
    ids = []
    for i in range(mesh.PolygonCount()):
        pol.SelectByIndex(i)
        ids.append(pol.ID())
    for i, pid in enumerate(ids):
        value = value_for(i)
        if value:
            pol.Select(pid)
            lx.object.StringTag(pol).Set(kind, value)


def tag_all(item, mat_for, part_for=None, pick_for=None):
    """One edit per tag type: setting a second tag type on a polygon in the
    same edit replaces the first one (seen in Modo 16 and 17)."""
    edit(item, lambda m: _tag_pass(m, lx.symbol.i_PTAG_MATR, mat_for))
    if part_for:
        edit(item, lambda m: _tag_pass(m, lx.symbol.i_PTAG_PART, part_for))
    if pick_for:
        edit(item, lambda m: _tag_pass(m, lx.symbol.i_PTAG_PICK, pick_for))


def box_maps(mesh):
    mm = mesh.MeshMapAccessor()
    try:
        mm.SelectByName(lx.symbol.i_VMAP_SUBDIV, 'Subdivision')
    except Exception:
        mm.New(lx.symbol.i_VMAP_SUBDIV, 'Subdivision')
    edge = mesh.EdgeAccessor()
    edge.SelectByIndex(0)
    st = lx.object.storage('f', 1)
    st.set((0.5,))
    edge.SetMapValue(mm.ID(), st)
    mm.New(lx.symbol.i_VMAP_WEIGHT, 'Weight')


def box_weights(mesh):
    # values written in the edit that creates a map are lost, so this is
    # a separate edit (writing them while creating it can also crash Modo)
    mm = mesh.MeshMapAccessor()
    mm.SelectByName(lx.symbol.i_VMAP_WEIGHT, 'Weight')
    pnt = mesh.PointAccessor()
    pnt.SelectByIndex(0)
    st = lx.object.storage('f', 1)
    st.set((1.0,))
    pnt.SetMapValue(mm.ID(), st)


def add_subd_quad(mesh):
    """A floating SubD quad next to the cube: headless Modo has no polygon
    selection, so mixed meshes are built through the API."""
    pnt = mesh.PointAccessor()
    ids = []
    for pos in ((1, 0, 0), (2, 0, 0), (2, 1, 0), (1, 1, 0)):
        ids.append(pnt.New(pos))
    st = lx.object.storage('p', 4)
    st.set(ids)
    mesh.PolygonAccessor().New(lx.symbol.iPTYP_SUBD, st, 4, 0)


def main():
    out = os.environ['HB_OUT']
    scene = modo.Scene()
    for item in list(scene.items('mesh')):
        scene.removeItems(item)

    lx.eval('item.create locator "Group A"')
    group = scene.selected[0]
    modo.Locator(group).position.set((0.0, 2.0, 0.0))

    box = new_prim('prim.cube', 'Box', ('segmentsX 2',))
    tag_all(box,
            lambda i: 'Red Paint' if i < 2 else 'Body',
            lambda i: 'Lid' if i < 2 else '',
            lambda i: {0: 'Set A', 1: 'Set A;Set B'}.get(i, ''))
    edit(box, box_maps)
    edit(box, box_weights)
    scene.select(box)
    lx.eval('select.typeFrom polygon')
    lx.eval('select.drop polygon')
    lx.eval('poly.convert face subpatch true')
    box.setParent(group)

    ball = new_prim('prim.sphere', 'Ball')
    ball.channel('visible').set('allOff')

    mixed = new_prim('prim.cube', 'Mixed')
    edit(mixed, add_subd_quad)
    tag_all(mixed, lambda i: 'Steel')

    lx.eval('select.typeFrom item')
    lx.eval('houdini.send "%s" ALL' % out)
    lx.out('HB| fixture done')


main()
