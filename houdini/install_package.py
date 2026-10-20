'''
Installs the Modo Bridge package for one Houdini version, on any OS.

    <houdini>/bin/hython install_package.py

It writes modo_bridge.json into that Houdini's user packages folder
(~/houdiniXX.X/packages, or Documents/houdiniXX.X/packages on Windows). That
file points at the folder this script is in. Run it once per Houdini version,
and again if you move the kit.
'''

import json
import os

import hou

root = os.path.dirname(os.path.abspath(__file__)).replace('\\', '/')
packages = os.path.join(hou.homeHoudiniDirectory(), 'packages')
if not os.path.isdir(packages):
    os.makedirs(packages)

package = {
    'env': [
        {'MODO_BRIDGE_ROOT': root},
        {'PYTHONPATH': {'value': '$MODO_BRIDGE_ROOT/python',
                        'method': 'append'}},
    ],
    'path': '$MODO_BRIDGE_ROOT',
}
target = os.path.join(packages, 'modo_bridge.json')
with open(target, 'w') as f:
    json.dump(package, f, indent=4)
print('Modo Bridge: wrote %s\n  -> %s\nRestart Houdini.' % (target, root))
