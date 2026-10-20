'''
Modo -> Houdini bridge -- Houdini side

Modo's "Send to Houdini" writes one USD file (default ~/modo_bridge/scene.usdc)
and then sends a one-line message to localhost:PORT. This module

  * creates nodes that read that file:
      create_lop()  a Sublayer LOP          -- for Solaris lighting/rendering
      create_sop()  a USD Import SOP        -- polygons with groups/attributes
  * listens for the message and presses Reload on every Sublayer LOP and
    USD Import SOP that reads the file (start_listener / stop_listener).

The listener starts by itself in graphical Houdini sessions (see pythonrc.py)
unless MODO_BRIDGE_AUTOSTART=0. Only one Houdini session can hold the port;
the others can still press Reload by hand.

Works with Houdini 21 and 22 (Python 3.11 / 3.13).
'''

import json
import os
import socket
import threading

import hou

PORT = int(os.environ.get('MODO_BRIDGE_PORT', '51234'))
DEFAULT_FILE = os.environ.get(
    'MODO_BRIDGE_FILE', os.path.join(os.path.expanduser('~'), 'modo_bridge',
                                     'scene.usdc'))

_server = None


def _log(msg):
    print('Modo Bridge: %s' % msg)


# ---------------------------------------------------------------------------
# nodes
# ---------------------------------------------------------------------------
def create_lop(parent=None, path=None):
    '''A Sublayer LOP reading the Modo file. Add lights, materials (at
    /materials/<tag> to replace Modo's stand-ins) and render settings after
    it; they survive every send from Modo.'''
    parent = parent or hou.node('/stage')
    node = parent.createNode('sublayer', 'modo_scene')
    node.parm('filepath1').set(path or DEFAULT_FILE)
    node.setComment('Modo Bridge: reloads when Modo sends')
    node.setGenericFlag(hou.nodeFlag.DisplayComment, True)
    node.moveToGoodPosition()
    return node


# Houdini imports non-overlapping USD subset families as string attributes
# (materialBind = "mat_Red", part = "Lid") and only overlapping ones
# (selection sets) as groups; this turns the first two back into groups.
GROUPS_VEX = '''\
string m = s@materialBind;
if (m != "") setprimgroup(0, m, @primnum, 1);
string p = s@part;
if (p != "") setprimgroup(0, "part_" + p, @primnum, 1);
'''


def create_sop(parent=None, path=None):
    '''A USD Import SOP that unpacks the Modo meshes to polygons, plus a
    wrangle that makes groups. Material tags, parts and selection sets end up
    as primitive groups (mat_*, part_*, set_*) and the string attributes
    modo_material / modo_part hold the exact Modo names. Returns the last
    node.'''
    if parent is None:
        parent = hou.node('/obj').createNode('geo', 'modo_geo')
    node = parent.createNode('usdimport', 'modo_import')
    node.parm('filepath1').set(path or DEFAULT_FILE)
    node.parm('primpattern').set('%type:Mesh')
    node.parm('input_unpack').set(1)
    node.parm('unpack_geomtype').set('polygons')
    node.setComment('Modo Bridge: reloads when Modo sends')
    node.setGenericFlag(hou.nodeFlag.DisplayComment, True)

    groups = parent.createNode('attribwrangle', 'modo_groups')
    groups.setInput(0, node)
    groups.parm('class').set('primitive')
    groups.parm('snippet').set(GROUPS_VEX)
    groups.setDisplayFlag(True)
    groups.setRenderFlag(True)
    parent.layoutChildren((node, groups))
    return groups


def _same_file(a, b):
    try:
        return os.path.realpath(hou.text.expandString(a)) == \
            os.path.realpath(b)
    except Exception:
        return False


def _inside_usdimport(node):
    parent = node.parent()
    while parent is not None:
        if parent.type().name() == 'usdimport':
            return True
        parent = parent.parent()
    return False


def bridge_nodes(path=None):
    '''Sublayer LOPs and USD Import SOPs that read `path`.'''
    path = path or DEFAULT_FILE
    found = []
    for type_name, cat in (('sublayer', hou.lopNodeTypeCategory()),
                           ('usdimport', hou.sopNodeTypeCategory())):
        node_type = hou.nodeType(cat, type_name)
        if node_type is None:
            continue
        for node in node_type.instances():
            if _inside_usdimport(node):
                continue        # the USD Import SOP's own helper network
            for parm in node.parms():
                if parm.name().startswith('filepath') and \
                        _same_file(parm.eval(), path):
                    found.append(node)
                    break
    return found


def reload_file(path=None):
    '''Press Reload on every node reading `path`. Returns how many.'''
    nodes = bridge_nodes(path)
    for node in nodes:
        try:
            node.parm('reload').pressButton()
        except Exception as exc:
            _log('could not reload %s (%s)' % (node.path(), exc))
    return len(nodes)


# ---------------------------------------------------------------------------
# listener
# ---------------------------------------------------------------------------
def _handle(message):
    try:
        data = json.loads(message)
    except ValueError:
        return
    if data.get('cmd') != 'reload':
        return
    path = data.get('file') or DEFAULT_FILE

    def run():
        n = reload_file(path)
        _log('reloaded %d node(s) for %s' % (n, path))

    try:
        import hdefereval
        hdefereval.executeDeferred(run)
    except ImportError:  # hython: no event loop, run here
        run()


class _Server(threading.Thread):
    def __init__(self, port):
        threading.Thread.__init__(self, name='modo_bridge', daemon=True)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(('127.0.0.1', port))
        self.sock.listen(4)
        self.running = True

    def run(self):
        while self.running:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                break
            with conn:
                conn.settimeout(2.0)
                chunks = []
                try:
                    while True:
                        data = conn.recv(65536)
                        if not data:
                            break
                        chunks.append(data)
                        if b'\n' in data:
                            break
                except OSError:
                    pass
                for line in b''.join(chunks).decode('utf-8', 'replace') \
                        .splitlines():
                    if line.strip():
                        _handle(line)
                try:
                    conn.sendall(b'ok\n')
                except OSError:
                    pass

    def stop(self):
        self.running = False
        try:
            self.sock.close()
        except OSError:
            pass


def start_listener(port=None, quiet=False):
    global _server
    if _server is not None:
        return True
    try:
        _server = _Server(port or PORT)
    except OSError as exc:
        if not quiet:
            _log('port %d is busy (%s); is another Houdini listening?'
                 % (port or PORT, exc))
        return False
    _server.start()
    _log('listening for Modo on 127.0.0.1:%d' % (port or PORT))
    return True


def stop_listener():
    global _server
    if _server is not None:
        _server.stop()
        _server = None
        _log('stopped listening')


def is_listening():
    return _server is not None


def autostart():
    if os.environ.get('MODO_BRIDGE_AUTOSTART', '1') == '0':
        return
    if hou.isUIAvailable():
        start_listener(quiet=True)
