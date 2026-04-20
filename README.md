# Smart-QGIS

Smart-QGIS is a QGIS plugin that runs an MCP server and interfaces with local LLMs to enable intelligent, chat-driven geospatial data processing and mapping.

## Installation

- Install QGIS as a prerequisite.
- Install uv to enable the MCP server to run within QGIS (it must be installed in the default location).
- Copy the plugin’s source code into the QGIS plugin directory.

  - Windows: `%APPDATA%\QGIS\QGIS3\profiles\default\python\plugins\`
  - macOS: `~/Library/Application Support/QGIS/QGIS3/profiles/default/python/plugins`
  - Linux: `~/.local/share/QGIS/QGIS3/profiles/default/python/plugins/`

## Add to other MCP client

You can also add the MCP server to other MCP client, such as VSCode, Qwen, etc., with the URL `http://127.0.0.1:8000/qgis`. Then you can interact with the QGIS instance from the third-party client.

## Main components

![UML of the main components](SmartQGIS-UML.png)


## If you want to contribute

Please implement the tools and its description in the `socket_server.py` and mcp_server.py file (description in English, but you can annotate the code in Chinese).

## What's next?

QGIS agent skills are on the way ...

## Thanks

This project is strongly inspired by [QGISMCP](https://github.com/jjsantos01/qgis_mcp) project.
