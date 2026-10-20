#python
'''
Modo -> Houdini bridge -- commands

    houdini.send  [filename] [items]
    houdini.items [ALL|SELECTED|?]
    houdini.file  [path|?]
    houdini.port  [int|?]

houdini.send reads the evaluated meshes (mesh ops and deformers applied) at
the current time, writes them as one USD file and tells a listening Houdini
to reload it. See houdini_bridge_usd.py for what ends up where.

Works in Modo 16.1 (Python 3.9) and Modo 17 (Python 3.10).
'''

import json
import os
import socket
import tempfile
import time
import traceback

import lx
import lxifc
import lxu.command
import modo

import houdini_bridge_usd as core


PREFS_FILE = 'houdini_bridge_prefs.json'

ITEMS_ITEMS = [
    ('ALL', 'All meshes'),
    ('SELECTED', 'Selected items (and their children)'),
]

DEFAULTS = {
    'items': 'ALL',
    'file': os.path.join(os.path.expanduser('~'), 'modo_bridge',
                         'scene.usdc'),
    'port': 51234,
}
VALID = {
    'items': [i[0] for i in ITEMS_ITEMS],
}

MESH_TYPES = ('mesh',)
HIDDEN = ('off', 'allOff')


def _log(msg):
    lx.out('Houdini bridge: %s' % msg)


# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------
def _prefs_path():
    for alias in ('user', 'temp'):
        try:
            base = lx.eval('query platformservice path.path ? %s' % alias)
            if base and os.path.isdir(base):
                return os.path.join(base, PREFS_FILE)
        except Exception:
            pass
    return os.path.join(tempfile.gettempdir(), PREFS_FILE)


def _prefs_read():
    try:
        with open(_prefs_path(), 'r') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def prefs_get(key):
    value = _prefs_read().get(key, DEFAULTS.get(key))
    allowed = VALID.get(key)
    if value is None or value == '' or (allowed and value not in allowed):
        return DEFAULTS.get(key)
    return value


def prefs_set(key, value):
    data = _prefs_read()
    data[key] = value
    try:
        with open(_prefs_path(), 'w') as f:
            json.dump(data, f, indent=2)
    except Exception as exc:
        _log('could not store settings (%s)' % exc)


class _PopupHints(lxifc.UIValueHints):
    def __init__(self, items):
        self._items = items

    def uiv_Flags(self):
        return lx.symbol.fVALHINT_POPUPS

    def uiv_PopCount(self):
        return len(self._items)

    def uiv_PopUserName(self, index):
        return self._items[index][1]

    def uiv_PopInternalName(self, index):
        return self._items[index][0]


class _Setting(lxu.command.BasicCommand):
    '''One queryable command per setting, for the popover.'''

    KEY = None
    ITEMS = None
    KIND = 'str'        # str or int

    def __init__(self):
        lxu.command.BasicCommand.__init__(self)
        self.dyna_Add('value', lx.symbol.sTYPE_INTEGER if self.KIND == 'int'
                      else lx.symbol.sTYPE_STRING)
        self.basic_SetFlags(0, lx.symbol.fCMDARG_OPTIONAL
                            | lx.symbol.fCMDARG_QUERY)

    def cmd_Flags(self):
        return lx.symbol.fCMD_UI

    def arg_UIValueHints(self, index):
        if index == 0 and self.ITEMS:
            return _PopupHints(self.ITEMS)
        return None

    def basic_Execute(self, msg, flags):
        if not self.dyna_IsSet(0):
            return
        if self.KIND == 'int':
            value = int(self.dyna_Int(0))
        else:
            value = self.dyna_String(0)
        if self.ITEMS:
            valid = [i[0] for i in self.ITEMS]
            if value not in valid:
                _log('%s: unknown value %r (expected one of %s)'
                     % (self.KEY, value, ', '.join(valid)))
                return
        prefs_set(self.KEY, value)

    def cmd_Query(self, index, vaQuery):
        try:
            if index == 0:
                va = lx.object.ValueArray()
                va.set(vaQuery)
                value = prefs_get(self.KEY)
                if self.KIND == 'int':
                    va.AddInt(int(value))
                else:
                    va.AddString(str(value))
        except Exception:
            pass
        return lx.result.OK


class ItemsSetting(_Setting):
    KEY = 'items'
    ITEMS = ITEMS_ITEMS


class FileSetting(_Setting):
    KEY = 'file'


class PortSetting(_Setting):
    KEY = 'port'
    KIND = 'int'


# ---------------------------------------------------------------------------
# reading the scene
# ---------------------------------------------------------------------------
def _internal(item):
    '''The lx.object.Item behind a modo.Item.'''
    return getattr(item, 'internalItem', None) or item._item


class Evaluator(object):
    '''Evaluated channel reads at one time.'''

    def __init__(self, scene, when):
        first = None
        for item in scene.items():
            first = _internal(item)
            break
        if first is None:
            raise core.ExportError('the scene is empty')
        self.read = lx.object.Scene(first.Context()).Channels(None, when)

    def world_matrix(self, item):
        it = _internal(item)
        mat = lx.object.Matrix(self.read.ValueObj(
            it, it.ChannelLookup('worldMatrix')))
        # row vectors, translation in the last row -- what USD uses too
        return [list(row) for row in mat.Get4()]

    def mesh(self, item):
        it = _internal(item)
        value = self.read.ValueObj(
            it, it.ChannelLookup(lx.symbol.sICHAN_MESH_MESH))
        try:
            return lx.object.Mesh(lx.object.MeshFilter(value).Generate())
        except Exception:
            return lx.object.Mesh(value)


def _ancestors(item):
    out = []
    parent = item.parent
    while parent is not None:
        out.append(parent)
        parent = parent.parent
    return out


def _descendants(item):
    try:
        return item.children(recursive=True)
    except Exception:
        return []


def _hidden(item):
    try:
        return str(item.channel('visible').get()) in HIDDEN
    except Exception:
        return False


def collect_items(scene, mode):
    '''Meshes to send plus every ancestor, parents first.'''
    if mode == 'SELECTED':
        pool = []
        for item in scene.selected:
            pool.append(item)
            pool.extend(_descendants(item))
    else:
        pool = list(scene.items('mesh'))
    meshes, seen = [], set()
    for item in pool:
        if item.type in MESH_TYPES and item.id not in seen:
            seen.add(item.id)
            meshes.append(item)

    ordered, done = [], set()
    for mesh in meshes:
        for it in reversed([mesh] + _ancestors(mesh)):
            if it.id not in done:
                done.add(it.id)
                ordered.append(it)
    return ordered, set(m.id for m in meshes)


def _maps(mesh, map_type):
    '''[(name, id)] of the vertex maps of one type. Enumerating maps on Modo
    17 fails on an unnamed internal map, so every step is guarded.'''
    found = []
    accessor = mesh.MeshMapAccessor()

    class Visitor(lxifc.Visitor):
        def vis_Evaluate(self):
            try:
                if accessor.Type() == map_type:
                    name = accessor.Name()
                    if name:
                        found.append((name, accessor.ID()))
            except Exception:
                pass

    try:
        accessor.FilterByType(map_type)
    except Exception:
        pass
    try:
        accessor.Enumerate(lx.symbol.iMARK_ANY, Visitor(), 0)
    except Exception:
        pass
    return found


def _poly_kinds():
    kinds = {}
    for name in ('FACE', 'SUBD', 'PSUB'):
        sym = getattr(lx.symbol, 'iPTYP_' + name, None)
        if sym is not None:
            kinds[sym] = name
    return kinds


def read_mesh(mesh, notes, label):
    '''MeshData (see houdini_bridge_usd.py) for an evaluated lx Mesh.'''
    pnt = mesh.PointAccessor()
    index_of, points = {}, []
    for i in range(mesh.PointCount()):
        pnt.SelectByIndex(i)
        index_of[pnt.ID()] = i
        points.append(tuple(pnt.Pos()))

    uv_maps = _maps(mesh, lx.symbol.i_VMAP_TEXTUREUV)
    rgb_maps = [(n, i, 3) for n, i in _maps(mesh, lx.symbol.i_VMAP_RGB)] + \
        [(n, i, 4) for n, i in _maps(mesh, lx.symbol.i_VMAP_RGBA)]
    normal_maps = _maps(mesh, lx.symbol.i_VMAP_NORMAL)
    weight_maps = _maps(mesh, lx.symbol.i_VMAP_WEIGHT)
    subd_maps = _maps(mesh, lx.symbol.i_VMAP_SUBDIV)

    kinds = _poly_kinds()
    pol = mesh.PolygonAccessor()
    tags = lx.object.StringTag(pol)
    s2 = lx.object.storage('f', 2)
    s3 = lx.object.storage('f', 3)
    s4 = lx.object.storage('f', 4)
    vnorm_id = normal_maps[0][1] if normal_maps else None

    faces, subd, mats, parts, sets = [], [], [], [], []
    uvs = [[] for _ in uv_maps]
    colors = [[] for _ in rgb_maps]
    normals = []
    skipped = 0

    def tag(kind):
        try:
            return tags.Get(kind) or ''
        except Exception:
            return ''

    for i in range(mesh.PolygonCount()):
        pol.SelectByIndex(i)
        kind = kinds.get(pol.Type())
        count = pol.VertexCount()
        if kind is None or count < 3:
            skipped += 1
            continue
        pid = pol.ID()
        ids = [pol.VertexByIndex(v) for v in range(count)]
        faces.append([index_of[p] for p in ids])
        smooth = kind != 'FACE'
        subd.append(smooth)
        mats.append(tag(lx.symbol.i_PTAG_MATR))
        part = tag(lx.symbol.i_PTAG_PART)
        parts.append('' if part == 'Default' else part)  # Modo's "no part"
        pick = tag(lx.symbol.i_PTAG_PICK)
        sets.append([s for s in pick.split(';') if s] if pick else [])

        for (name, mid), out in zip(uv_maps, uvs):
            for p in ids:
                out.append(tuple(s2.get()) if pol.MapEvaluate(mid, p, s2)
                           else (0.0, 0.0))
        for (name, mid, size), out in zip(rgb_maps, colors):
            st = s4 if size == 4 else s3
            for p in ids:
                out.append(tuple(st.get()) if pol.MapEvaluate(mid, p, st)
                           else (1.0,) * size)
        for p in ids:
            if smooth:
                normals.append((0.0, 0.0, 1.0))    # unused on SubD
            elif vnorm_id is not None and pol.MapEvaluate(vnorm_id, p, s3):
                normals.append(tuple(s3.get()))
            else:
                # honours the material's smoothing angle, like Modo shades
                pnt.Select(p)
                normals.append(tuple(pnt.Normal(pid)))

    weights = []
    s1 = lx.object.storage('f', 1)
    for name, mid in weight_maps:
        vals = []
        for i in range(len(points)):
            pnt.SelectByIndex(i)
            vals.append(s1.get()[0] if pnt.MapEvaluate(mid, s1) else 0.0)
        weights.append((name, vals))

    creases, corners = [], []
    if subd_maps and any(subd):
        sid = subd_maps[0][1]
        edge = mesh.EdgeAccessor()
        for i in range(mesh.EdgeCount()):
            edge.SelectByIndex(i)
            if edge.MapEvaluate(sid, s1):
                a, b = edge.Endpoints()
                if a in index_of and b in index_of:
                    creases.append(((index_of[a], index_of[b]), s1.get()[0]))
        for i in range(len(points)):
            pnt.SelectByIndex(i)
            if pnt.MapEvaluate(sid, s1) and s1.get()[0] > 0:
                corners.append((i, s1.get()[0]))

    if skipped:
        notes.append('%s: skipped %d curve/line polygon(s)' % (label, skipped))
    return {
        'points': points, 'faces': faces, 'subd': subd,
        'material': mats, 'part': parts, 'sets': sets,
        'uvs': [(name, vals) for (name, _), vals in zip(uv_maps, uvs)],
        'normals': normals if not all(subd) else None,
        'colors': [(name, size, vals)
                   for (name, _, size), vals in zip(rgb_maps, colors)],
        'weights': weights,
        'creases': creases,
        'corners': corners,
    }


def _channel(item, name, default):
    try:
        value = item.channel(name).get()
        return default if value is None else value
    except Exception:
        return default


def _material_of(mask):
    '''First Modo material under a shader tree mask.'''
    for child in _descendants(mask):
        if child.type == 'advancedMaterial':
            return child
    return None


def read_materials(scene, tags):
    '''MaterialData per material tag, from the shader tree masks.'''
    masks = {}
    for mask in scene.items('mask'):
        ptyp = str(_channel(mask, 'ptyp', ''))
        ptag = str(_channel(mask, 'ptag', ''))
        if ptag and ptyp in ('Material', '') and ptag not in masks:
            mat = _material_of(mask)
            if mat is not None:
                masks[ptag] = mat
    out = {}
    for tag in tags:
        mat = masks.get(tag)
        if mat is None:
            out[tag] = {}
            continue
        amt = float(_channel(mat, 'diffAmt', 1.0))
        color = tuple(min(1.0, float(_channel(mat, 'diffCol.' + c, 0.6)) * amt)
                      for c in 'RGB')
        out[tag] = {
            'color': color,
            'roughness': float(_channel(mat, 'rough', 0.5)),
            'metallic': float(_channel(mat, 'metallic', 0.0)),
            'opacity': 1.0 - float(_channel(mat, 'tranAmt', 0.0)),
        }
    return out


def read_scene(mode):
    scene = modo.Scene()
    when = lx.service.Selection().GetTime()
    ev = Evaluator(scene, when)
    items, mesh_ids = collect_items(scene, mode)
    if not mesh_ids:
        raise core.ExportError(
            'no meshes to send' + (' (nothing selected?)'
                                   if mode == 'SELECTED' else ''))
    notes, data, tags = [], [], set()
    for item in items:
        parent = item.parent
        entry = {
            'id': item.id,
            'parent': parent.id if parent is not None else None,
            'name': item.name,
            'matrix': ev.world_matrix(item),
            'hidden': _hidden(item),
            'mesh': None,
        }
        if item.id in mesh_ids:
            mesh = read_mesh(ev.mesh(item), notes, item.name)
            entry['mesh'] = mesh
            tags.update(t or core.DEFAULT_TAG for t in mesh['material'])
        data.append(entry)
    return {'items': data, 'materials': read_materials(scene, tags)}, notes


# ---------------------------------------------------------------------------
# houdini.send
# ---------------------------------------------------------------------------
def ping_houdini(path, port):
    '''Ask a listening Houdini to reload `path`. True if one answered.'''
    msg = json.dumps({'cmd': 'reload', 'file': path}) + '\n'
    try:
        conn = socket.create_connection(('127.0.0.1', int(port)), timeout=0.5)
    except OSError:
        return False
    try:
        conn.settimeout(2.0)
        conn.sendall(msg.encode('utf-8'))
        conn.shutdown(socket.SHUT_WR)
        return conn.recv(16).startswith(b'ok')
    except OSError:
        return False
    finally:
        conn.close()


def send(path, mode, port):
    t0 = time.time()
    scene, notes = read_scene(mode)
    t1 = time.time()
    ascii = path.lower().endswith('.usda')
    stats = core.write_stage(path, scene, ascii=ascii)
    t2 = time.time()
    heard = ping_houdini(path, port)
    for note in notes:
        _log(note)
    _log('%d mesh(es), %d faces, %d material tag(s) -> %s '
         '(read %.2fs, write %.2fs); %s'
         % (stats['meshes'], stats['faces'], stats['materials'], path,
            t1 - t0, t2 - t1,
            'Houdini reloaded' if heard else
            'no Houdini listening on port %s, press Reload there' % port))
    return stats, heard


class SendToHoudini(lxu.command.BasicCommand):

    def __init__(self):
        lxu.command.BasicCommand.__init__(self)
        filepath = getattr(lx.symbol, 'sTYPE_FILEPATH',
                           lx.symbol.sTYPE_STRING)
        self.dyna_Add('filename', filepath)
        self.dyna_Add('items', lx.symbol.sTYPE_STRING)
        for index in range(2):
            self.basic_SetFlags(index, lx.symbol.fCMDARG_OPTIONAL)

    def cmd_Flags(self):
        # reads the scene and writes a file, nothing to undo
        return lx.symbol.fCMD_UI

    def basic_Enable(self, msg):
        return True

    def arg_UIValueHints(self, index):
        return _PopupHints(ITEMS_ITEMS) if index == 1 else None

    def basic_Execute(self, msg, flags):
        path = self.dyna_String(0) if self.dyna_IsSet(0) else \
            prefs_get('file')
        path = os.path.abspath(os.path.expanduser(path))
        mode = (self.dyna_String(1) if self.dyna_IsSet(1)
                else prefs_get('items')).upper()
        try:
            send(path, mode, prefs_get('port'))
        except core.ExportError as exc:
            self._fail(str(exc))
        except Exception as exc:
            _log(traceback.format_exc())
            self._fail('send failed: %s' % exc)

    def _fail(self, message):
        _log(message)
        try:
            modo.dialogs.alert('Send to Houdini', message, dtype='error')
        except Exception:
            pass

    def cmd_Query(self, index, vaQuery):
        lx.notimpl()


lx.bless(ItemsSetting, 'houdini.items')
lx.bless(FileSetting, 'houdini.file')
lx.bless(PortSetting, 'houdini.port')
lx.bless(SendToHoudini, 'houdini.send')
