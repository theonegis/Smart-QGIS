import os
import subprocess
import shutil
from pathlib import Path
from qgis.PyQt import QtCore
from qgis.PyQt.QtWidgets import QAction
from qgis.PyQt.QtGui import QIcon
from qgis.core import QgsMessageLog, Qgis
from .chat_widget import ChatDockWidget
from .socket_server import RequestHandler, QgisSocketServer
from .mcp_client import McpClient


class QGISChatPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.action = None
        self.dock = None
        self.port = 9876
        self.log_tag = "Smart QGIS"

        self.plugin_dir = Path(__file__).parent
        self.socket_server = None
        self.handler = None
        self.mcp_client = None
        self.mcp_process = None

    def initGui(self):
        """called when the plugin is loaded"""
        QgsMessageLog.logMessage(
            "Loading Smart QGIS Plugin...", self.log_tag, Qgis.Info
        )

        icon = self.plugin_dir / "resources" / "logo.png"
        self.action = QAction(QIcon(str(icon)), "&Smart QGIS", self.iface.mainWindow())
        self.action.triggered.connect(self.open_chat)
        plugins_menu = self.iface.pluginMenu()
        plugins_menu.addAction(self.action)

        # Start Servers
        self.start_socket_server()
        self.start_mcp_server()

        QgsMessageLog.logMessage(
            "Smart QGIS Plugin loaded successfully", self.log_tag, Qgis.Info
        )

    def is_socket_server_running(self):
        return self.socket_server is not None and self.socket_server.isRunning()

    def is_mcp_server_running(self):
        return self.mcp_process is not None and self.mcp_process.poll() is None

    def start_socket_server(self):
        if self.socket_server:
            QgsMessageLog.logMessage(
                "Socket Server already running", self.log_tag, Qgis.Warning
            )
            return

        QgsMessageLog.logMessage("Starting Socket Server...", self.log_tag, Qgis.Info)
        try:
            self.handler = RequestHandler(self.iface)
            self.socket_server = QgisSocketServer(self.handler)
            self.socket_server.start()
            QgsMessageLog.logMessage("Socket Server started", self.log_tag, Qgis.Info)
        except Exception as e:
            QgsMessageLog.logMessage(
                f"Failed to start Socket Server: {e}", self.log_tag, Qgis.Critical
            )

    def stop_socket_server(self):
        if not self.socket_server:
            return

        QgsMessageLog.logMessage("Stopping Socket Server...", self.log_tag, Qgis.Info)
        self.socket_server.stop()
        if not self.socket_server.wait(1000):
            QgsMessageLog.logMessage(
                "Socket Server did not stop within timeout", self.log_tag, Qgis.Warning
            )
        else:
            QgsMessageLog.logMessage("Socket Server stopped", self.log_tag, Qgis.Info)
        self.socket_server = None

    def stop_mcp_server(self):
        if not self.mcp_process:
            return

        QgsMessageLog.logMessage("Stopping MCP Server...", self.log_tag, Qgis.Info)
        try:
            self.mcp_process.terminate()
            try:
                self.mcp_process.wait(timeout=1)
                QgsMessageLog.logMessage(
                    "MCP Server terminated gracefully", self.log_tag, Qgis.Info
                )
            except subprocess.TimeoutExpired:
                self.mcp_process.kill()
                self.mcp_process.wait()
                QgsMessageLog.logMessage(
                    "MCP Server forcefully killed", self.log_tag, Qgis.Info
                )
        except Exception as e:
            QgsMessageLog.logMessage(
                f"Error stopping MCP Server: {e}", self.log_tag, Qgis.Warning
            )
            try:
                self.mcp_process.kill()
                self.mcp_process.wait()
            except:
                pass

        # Close pipes
        for pipe in (
            self.mcp_process.stdout,
            self.mcp_process.stderr,
            self.mcp_process.stdin,
        ):
            try:
                if pipe and not pipe.closed:
                    pipe.close()
            except:
                pass

        self.mcp_process = None
        self.mcp_client = None
        # Notify dock if it exists
        if self.dock:
            self.dock.on_mcp_server_changed(None)

    def unload(self):
        """called when the plugin is unloaded"""
        QgsMessageLog.logMessage(
            "Unloading Smart QGIS Plugin...", self.log_tag, Qgis.Info
        )

        # Stop Socket Server
        self.stop_socket_server()
        self.stop_mcp_server()

        # Remove from Plugins menu and toolbar
        if self.action:
            plugins_menu = self.iface.pluginMenu()
            if plugins_menu:
                plugins_menu.removeAction(self.action)
            self.iface.removeToolBarIcon(self.action)

        if self.dock:
            self.iface.removeDockWidget(self.dock)

        QgsMessageLog.logMessage(
            "Smart QGIS Plugin unloaded successfully", self.log_tag, Qgis.Info
        )

    def start_mcp_server(self):
        if self.mcp_process:
            QgsMessageLog.logMessage(
                "MCP Server already running", self.log_tag, Qgis.Warning
            )
            return

        server_dir = self.plugin_dir / "server"
        mcp_script = "mcp_server.py"
        # Find uv executable
        uv_path = shutil.which("uv")
        if not uv_path:
            possible_path = Path(
                f"~/.local/bin/uv{'.exe' if os.name == 'nt' else ''}"
            ).expanduser()
            if possible_path.exists():
                uv_path = str(possible_path)
                QgsMessageLog.logMessage(f"uv path: {uv_path}", self.log_tag, Qgis.Info)

        if not uv_path:
            self.on_mcp_server_error(
                "uv executable not found. Please install uv or ensure it is in your system PATH."
            )
            return

        try:
            # We run it as a subprocess using uv
            # IMPORTANT: QGIS sets PYTHONHOME and PYTHONPATH which can confuse the subprocess
            # if it tries to use a different Python interpreter (like the one uv manages).
            # We must clear these to let the subprocess find its own standard library.
            env = os.environ.copy()
            env.pop("PYTHONHOME", None)
            env.pop("PYTHONPATH", None)

            # Check if .venv exists, if not run uv sync
            venv_path = server_dir / ".venv"
            if not venv_path.exists():
                QgsMessageLog.logMessage(
                    "Virtual environment not found. Initializing with uv sync...",
                    self.log_tag,
                    Qgis.Info,
                )
                sync_cmd = [uv_path, "sync"]
                try:
                    subprocess.run(
                        sync_cmd,
                        cwd=str(server_dir),
                        env=env,
                        check=True,
                        capture_output=True,
                        text=True,
                    )
                    QgsMessageLog.logMessage(
                        "Virtual environment initialized successfully.",
                        self.log_tag,
                        Qgis.Info,
                    )
                except subprocess.CalledProcessError as e:
                    self.on_mcp_server_error(
                        f"Failed to initialize virtual environment: {e.stderr}"
                    )
                    return

            cmd = [uv_path, "run", mcp_script]
            QgsMessageLog.logMessage(
                f"Starting MCP Server with uv: {cmd}", self.log_tag, Qgis.Info
            )

            process = subprocess.Popen(
                cmd,
                cwd=str(server_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.PIPE,
                text=True,
                bufsize=1,
                env=env,
            )
            QgsMessageLog.logMessage(
                f"Started MCP Server with PID {process.pid}", self.log_tag, Qgis.Info
            )
            self.on_mcp_server_started(process)

        except Exception as e:
            self.on_mcp_server_error(f"Failed to start MCP Server: {e}")

    def on_mcp_server_started(self, process):
        self.mcp_process = process
        # Initialize Client
        try:
            self.mcp_client = McpClient(self.mcp_process)
            QgsMessageLog.logMessage("MCP Client initialized", self.log_tag, Qgis.Info)
            # Notify dock if it exists
            if self.dock:
                self.dock.on_mcp_server_changed(self.mcp_client)
        except Exception as e:
            self.on_mcp_server_error(f"Failed to initialize MCP Client: {e}")
            # Ensure we clean up the process if client init fails
            self.stop_mcp_server()

    def on_mcp_server_error(self, error_msg):
        QgsMessageLog.logMessage(error_msg, self.log_tag, Qgis.Critical)
        # self.iface.messageBar().pushMessage("Error", error_msg, level=3)

    def open_chat(self):
        if not self.dock:
            self.dock = ChatDockWidget(self.iface.mainWindow())
            # Connect the signals or pass callbacks
            self.dock.set_server_controller(self)
            self.dock.on_mcp_server_changed(self.mcp_client)
            self.iface.addDockWidget(QtCore.Qt.RightDockWidgetArea, self.dock)

        self.dock.show()
        self.dock.raise_()
