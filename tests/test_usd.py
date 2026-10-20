'''
Tests for the USD writer, without Modo. Run with hython:

    /opt/hfs22.0/bin/hython tests/test_usd.py

Builds synthetic scenes, writes them, reads them back with pxr and imports
them into SOPs the way the Houdini package does.
'''

import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'lxserv'))
sys.path.insert(0, os.path.join(HERE, '..', 'houdini', 'python'))

import houdini_bridge_usd as core  # noqa: E402
from pxr import Usd, UsdGeom, UsdShade  # noqa: E402

FAILS = []


def check(cond, msg):
    print(('ok    ' if cond else 'FAIL  ') + msg)
    if not cond:
        FAILS.append(msg)


IDENT = [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]


def box_mesh(subd_flags, materials, parts, sets):
    '''A 2x1 strip of quads: faces 0 and 1.'''
    pts = [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0), (2, 0, 0), (2, 1, 0)]
    faces = [[0, 1, 2, 3], [1, 4, 5, 2]]
    fv = [p for f in faces for p in f]
    return {
        'points': pts, 'faces': faces, 'subd': subd_flags,
        'material': materials, 'part': parts, 'sets': sets,
        'uvs': [('Texture', [(pts[p][0] / 2.0, pts[p][1]) for p in fv]),
                ('Second UV', [(0.5, 0.5)] * len(fv))],
        'normals': [(0, 0, 1)] * len(fv),
        'colors': [('Color', 4, [(1, 0, 0, 1)] * len(fv))],
        'weights': [('Weight', [0.0, 0.5, 1.0, 0.0, 0.25, 0.75])],
        'creases': [((1, 2), 1.0), ((0, 1), 0.0)],
        'corners': [(0, 0.5)],
    }


def scene():
    moved = [r[:] for r in IDENT]
    moved[3] = [0, 2, 0, 1]
    child = [r[:] for r in IDENT]
    child[3] = [1, 2, 0, 1]          # world; local should be (1, 0, 0)
    return {
        'items': [
            {'id': 'g', 'parent': None, 'name': 'Group 1', 'matrix': moved,
             'hidden': False, 'mesh': None},
            {'id': 'a', 'parent': 'g', 'name': 'Box', 'matrix': child,
             'hidden': False,
             'mesh': box_mesh([True, True], ['Red', 'Blue'], ['Lid', ''],
                              [['Set A'], ['Set A', 'Set B']])},
            {'id': 'b', 'parent': 'g', 'name': 'Box', 'matrix': child,
             'hidden': True,
             'mesh': box_mesh([True, False], ['Red', ''], ['', ''],
                              [[], []])},
        ],
        'materials': {'Red': {'color': (0.8, 0.1, 0.1), 'roughness': 0.3},
                      'Blue': {'color': (0.1, 0.1, 0.8)},
                      'Default': {}},
    }


def test_names():
    check(core.prim_name('My Mat.01') == 'My_Mat_01', 'prim_name cleans')
    check(core.prim_name('3d') == '_3d', 'prim_name leading digit')
    check(core.prim_name('***') == 'item', 'prim_name fallback')
    t = core.NameTable()
    check([t.take('a b'), t.take('a_b'), t.take('a.b')] ==
          ['a_b', 'a_b_1', 'a_b_2'], 'NameTable makes siblings unique')


def test_stage(path):
    stats = core.write_stage(path, scene())
    check(stats['meshes'] == 3 and stats['faces'] == 4,
          'stats %r' % stats)
    st = Usd.Stage.Open(path)
    check(UsdGeom.GetStageMetersPerUnit(st) == 1.0, 'metres')
    check(UsdGeom.GetStageUpAxis(st) == 'Y', 'Y up')
    check(st.GetDefaultPrim().GetPath() == '/modo', 'default prim /modo')

    box = st.GetPrimAtPath('/modo/Group_1/Box')
    check(box.IsValid(), 'item path /modo/Group_1/Box')
    m = UsdGeom.Xformable(box).GetLocalTransformation()
    check(tuple(m.ExtractTranslation()) == (1, 0, 0),
          'local transform from world matrices: %r'
          % (tuple(m.ExtractTranslation()),))
    mesh = UsdGeom.Mesh(st.GetPrimAtPath('/modo/Group_1/Box/Box'))
    check(mesh.GetSubdivisionSchemeAttr().Get() == 'catmullClark', 'subd')
    check(list(mesh.GetCreaseIndicesAttr().Get()) == [1, 2] and
          list(mesh.GetCreaseSharpnessesAttr().Get()) == [10.0],
          'creases: zero weights dropped, 100% -> 10')
    check(list(mesh.GetCornerSharpnessesAttr().Get()) == [5.0], 'corners')
    pv = UsdGeom.PrimvarsAPI(mesh)
    check(pv.GetPrimvar('st').IsIndexed() and
          pv.GetPrimvar('Second_UV').IsDefined(), 'UV maps st + Second_UV')
    check(not pv.GetPrimvar('normals').IsDefined(), 'no normals on SubD')
    check(list(pv.GetPrimvar('modo_material').Get()) == ['Red', 'Blue'],
          'modo_material per face')
    check(list(pv.GetPrimvar('modo_part').Get()) == ['Lid', ''],
          'modo_part per face')

    subs = {s.GetPrim().GetName(): s for s in
            UsdGeom.Subset.GetAllGeomSubsets(UsdGeom.Imageable(mesh))}
    check(sorted(subs) == ['mat_Blue', 'mat_Red', 'part_Lid', 'set_Set_A',
                           'set_Set_B'], 'subsets %s' % sorted(subs))
    check(list(subs['set_Set_A'].GetIndicesAttr().Get()) == [0, 1],
          'selection sets overlap')
    bound = UsdShade.MaterialBindingAPI(subs['mat_Red'].GetPrim()) \
        .GetDirectBinding().GetMaterialPath()
    check(str(bound) == '/materials/Red', 'subset bound to /materials/Red')
    shader = UsdShade.Shader(st.GetPrimAtPath('/materials/Red/preview'))
    check(tuple(shader.GetInput('diffuseColor').Get()) ==
          tuple(float(x) for x in __import__('pxr').Gf.Vec3f(0.8, 0.1, 0.1)),
          'stand-in material colour')

    # second "Box": unique name, hidden, mixed SubD/faces split in two
    check(st.GetPrimAtPath('/modo/Group_1/Box_1').IsValid(),
          'duplicate item names get a suffix')
    check(UsdGeom.Imageable(st.GetPrimAtPath('/modo/Group_1/Box_1'))
          .ComputeVisibility() == 'invisible', 'hidden item invisible')
    a = UsdGeom.Mesh(st.GetPrimAtPath('/modo/Group_1/Box_1/Box_1'))
    b = UsdGeom.Mesh(st.GetPrimAtPath('/modo/Group_1/Box_1/Box_1_faces'))
    check(a and b, 'mixed mesh split into SubD + _faces')
    check(len(a.GetPointsAttr().Get()) == 4 and
          len(b.GetPointsAttr().Get()) == 4, 'split meshes compact points')
    check(b.GetSubdivisionSchemeAttr().Get() == 'none' and
          UsdGeom.PrimvarsAPI(b).GetPrimvar('normals').IsDefined(),
          'faces part: no subdivision, has normals')
    check(list(UsdGeom.PrimvarsAPI(b).GetPrimvar('modo_material').Get())
          == ['Default'], 'untagged faces become Default')


def test_houdini_sops(path):
    import hou
    import modo_bridge
    geo = hou.node('/obj').createNode('geo')
    sop = modo_bridge.create_sop(geo, path)
    g = sop.geometry()
    groups = sorted(x.name() for x in g.primGroups())
    check(len(g.prims()) == 4, 'SOP import: 4 polygons (%d)' % len(g.prims()))
    check({'mat_Red', 'mat_Blue', 'part_Lid', 'set_Set_A'} <= set(groups),
          'SOP groups %s' % groups)
    names = [p.attribValue('modo_material') for p in g.prims()]
    check(sorted(names) == ['Blue', 'Default', 'Red', 'Red'],
          'SOP modo_material %s' % names)
    check(g.findVertexAttrib('uv') is not None, 'SOP uv attribute')

    lop = modo_bridge.create_lop(hou.node('/stage'), path)
    st = lop.stage()
    check(st.GetPrimAtPath('/modo/Group_1/Box/Box').IsValid(),
          'LOP sublayer loads the file')
    # rewrite with a renamed tag, reload, check the stage follows
    s = scene()
    s['items'][1]['mesh']['material'] = ['Green', 'Blue']
    s['materials']['Green'] = {'color': (0, 1, 0)}
    core.write_stage(path, s)
    n = modo_bridge.reload_file(path)
    st = lop.stage()
    check(n == 2, 'reload_file found both nodes (%d)' % n)
    check(st.GetPrimAtPath('/modo/Group_1/Box/Box/mat_Green').IsValid(),
          'LOP sees the new file after reload')


def main():
    folder = tempfile.mkdtemp(prefix='hb_test_')
    path = os.path.join(folder, 'scene.usdc')
    test_names()
    test_stage(path)
    try:
        import hou  # noqa: F401
    except ImportError:
        print('skip  Houdini node tests (not in hython)')
    else:
        test_houdini_sops(path)
    print('\n%d failure(s)' % len(FAILS))
    sys.exit(1 if FAILS else 0)


if __name__ == '__main__':
    main()
