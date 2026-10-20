'''
Checks a file written by modo_fixture.py. Run with hython:

    hython tests/check_e2e.py <file.usd>
'''

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'houdini', 'python'))

from pxr import Usd, UsdGeom, UsdShade  # noqa: E402

FAILS = []


def check(cond, msg):
    print(('ok    ' if cond else 'FAIL  ') + msg)
    if not cond:
        FAILS.append(msg)


def subsets(mesh):
    return {s.GetPrim().GetName(): list(s.GetIndicesAttr().Get())
            for s in UsdGeom.Subset.GetAllGeomSubsets(UsdGeom.Imageable(mesh))}


def main(path):
    st = Usd.Stage.Open(path)
    box = UsdGeom.Mesh(st.GetPrimAtPath('/modo/Group_A/Box/Box'))
    check(bool(box), 'Box under Group_A')
    xf = UsdGeom.Xformable(st.GetPrimAtPath('/modo/Group_A'))
    check(tuple(xf.GetLocalTransformation().ExtractTranslation()) ==
          (0, 2, 0), 'Group A moved +2 Y')
    check(box.GetSubdivisionSchemeAttr().Get() == 'catmullClark',
          'Box is SubD')
    n = len(box.GetFaceVertexCountsAttr().Get())
    subs = subsets(box)
    print('      Box subsets:', {k: len(v) for k, v in subs.items()})
    check(subs.get('mat_Red_Paint') == [0, 1], 'mat_Red_Paint = faces 0, 1')
    check(len(subs.get('mat_Body', [])) == n - 2, 'mat_Body = the rest')
    check(subs.get('part_Lid') == [0, 1], 'part_Lid')
    check(subs.get('set_Set_A') == [0, 1] and subs.get('set_Set_B') == [1],
          'selection sets A and B')
    pv = UsdGeom.PrimvarsAPI(box)
    mats = list(pv.GetPrimvar('modo_material').Get())
    check(mats[:2] == ['Red Paint'] * 2, 'modo_material keeps the space')
    check(pv.GetPrimvar('st').IsDefined(), 'Box has UVs (st)')
    check(pv.GetPrimvar('Weight').IsDefined() and
          max(pv.GetPrimvar('Weight').Get()) == 1.0, 'weight map')
    sharp = box.GetCreaseSharpnessesAttr().Get()
    check(sharp is not None and list(sharp) == [5.0],
          'edge weight 50%% -> crease 5 (%s)' % (sharp,))
    bound = UsdShade.MaterialBindingAPI(
        st.GetPrimAtPath('/modo/Group_A/Box/Box/mat_Red_Paint')) \
        .GetDirectBinding().GetMaterialPath()
    check(str(bound) == '/materials/Red_Paint', 'bound to stand-in')
    check(st.GetPrimAtPath('/materials/Red_Paint').GetCustomDataByKey(
        'modo:tag') == 'Red Paint', 'material keeps Modo tag name')

    ball = UsdGeom.Mesh(st.GetPrimAtPath('/modo/Ball/Ball'))
    check(bool(ball) and ball.GetSubdivisionSchemeAttr().Get() == 'none',
          'Ball is plain polygons')
    check(UsdGeom.Imageable(ball).ComputeVisibility() == 'invisible',
          'Ball hidden')
    normals = UsdGeom.PrimvarsAPI(ball).GetPrimvar('normals')
    check(normals.IsDefined() and len(normals.Get()) > 100,
          'Ball has smooth normals (%d unique)' % len(normals.Get() or []))
    check(set(UsdGeom.PrimvarsAPI(ball).GetPrimvar('modo_material').Get())
          == {'Default'}, 'untagged -> Default')

    a = UsdGeom.Mesh(st.GetPrimAtPath('/modo/Mixed/Mixed'))
    b = UsdGeom.Mesh(st.GetPrimAtPath('/modo/Mixed/Mixed_faces'))
    check(bool(a) and bool(b), 'Mixed split in Mixed + Mixed_faces')
    if a and b:
        check(len(a.GetFaceVertexCountsAttr().Get()) == 1 and
              len(b.GetFaceVertexCountsAttr().Get()) == 6,
              '1 SubD face + 6 plain faces')

    # winding: Box face normals must point away from its centre
    pts = box.GetPointsAttr().Get()
    counts = box.GetFaceVertexCountsAttr().Get()
    idx = box.GetFaceVertexIndicesAttr().Get()
    centre = [sum(p[k] for p in pts) / len(pts) for k in range(3)]
    out_ok, o = True, 0
    for c in counts:
        f = [pts[i] for i in idx[o:o + c]]
        o += c
        u = [f[1][k] - f[0][k] for k in range(3)]
        v = [f[2][k] - f[0][k] for k in range(3)]
        nrm = (u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2],
               u[0] * v[1] - u[1] * v[0])
        mid = [sum(p[k] for p in f) / c - centre[k] for k in range(3)]
        if sum(nrm[k] * mid[k] for k in range(3)) <= 0:
            out_ok = False
    check(out_ok, 'counter-clockwise faces point outwards (rightHanded)')

    try:
        import hou
    except ImportError:
        hou = None
    if hou is not None:
        import modo_bridge
        g = modo_bridge.create_sop(None, path).geometry()
        groups = set(x.name() for x in g.primGroups())
        print('      SOP groups:', sorted(groups))
        check({'mat_Red_Paint', 'mat_Body', 'part_Lid', 'set_Set_A',
               'set_Set_B', 'mat_Default', 'mat_Steel'} <= groups,
              'SOP groups for tags, parts and sets')
        check(g.findPrimAttrib('modo_material') is not None,
              'SOP modo_material attribute')
    print('\n%d failure(s)' % len(FAILS))
    return 1 if FAILS else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1]))
