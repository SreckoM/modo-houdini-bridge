# Modo Bridge: start listening for Modo's "Send to Houdini" (graphical
# sessions only; set MODO_BRIDGE_AUTOSTART=0 to turn this off).
try:
    import modo_bridge
    modo_bridge.autostart()
except Exception as exc:
    print('Modo Bridge: not started (%s)' % exc)
