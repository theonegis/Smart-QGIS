"""
QGIS requires that this file contains a classFactory() function, which is called when the plugin gets loaded into QGIS
"""

def classFactory(iface):
    from .chat_plugin import QGISChatPlugin
    return QGISChatPlugin(iface)