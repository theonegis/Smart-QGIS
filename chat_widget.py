from qgis.PyQt import QtWidgets
from qgis.PyQt import QtCore
from qgis.PyQt.QtCore import pyqtSlot
from qgis.PyQt.QtGui import QIcon, QTextCursor
from qgis.core import QgsMessageLog, Qgis
from pathlib import Path
import requests
import json
import re
from .styles import (
    STYLE_ROUNDED_CORNER,
    STYLE_BUTTON,
    STYLE_STOP_BUTTON,
    STYLE_SERVER_BTN_ON,
    STYLE_SERVER_BTN_OFF,
    get_combobox_style,
)
from .prompts import QGIS_AGENT_SYSTEM_PROMPT

LOG_TAG = "Smart QGIS"


class ModelFetcherThread(QtCore.QThread):
    """Thread to fetch models from Ollama."""

    models_fetched = QtCore.pyqtSignal(list)
    error_occurred = QtCore.pyqtSignal(str)

    def run(self):
        QgsMessageLog.logMessage(
            "ModelFetcherThread: Starting run()", LOG_TAG, Qgis.Info
        )
        try:
            response = requests.get("http://localhost:11434/api/tags")
            QgsMessageLog.logMessage(
                f"ModelFetcherThread: Response status {response.status_code}",
                LOG_TAG,
                Qgis.Info,
            )
            if response.status_code == 200:
                data = response.json()
                models = [model["name"] for model in data.get("models", [])]
                QgsMessageLog.logMessage(
                    f"ModelFetcherThread: Models found {models}", LOG_TAG, Qgis.Info
                )
                self.models_fetched.emit(models)
            else:
                raise Exception(f"Status code: {response.status_code}")
        except Exception as e:
            QgsMessageLog.logMessage(
                f"ModelFetcherThread: Error {e}", LOG_TAG, Qgis.Critical
            )
            self.error_occurred.emit(str(e))


class ChatWorker(QtCore.QObject):
    """Worker to handle chat interaction with Ollama, including tool calling."""

    chunk_received = QtCore.pyqtSignal(str)
    finished = QtCore.pyqtSignal()
    error = QtCore.pyqtSignal(str)
    tool_status = QtCore.pyqtSignal(str)

    def __init__(self, model, messages, mcp_client=None):
        super().__init__()
        self.model = model
        self.messages = messages
        self.mcp_client = mcp_client

    def run(self):
        self.is_running = True
        try:
            tools = []
            if self.mcp_client:
                try:
                    mcp_tools = self.mcp_client.list_tools()
                    tools = self._convert_tools_to_ollama(mcp_tools)
                except Exception as e:
                    QgsMessageLog.logMessage(
                        f"Failed to fetch tools: {e}", LOG_TAG, Qgis.Warning
                    )

            QgsMessageLog.logMessage(
                f"ChatWorker started. Tools available: {len(tools)}", LOG_TAG, Qgis.Info
            )
            if tools:
                # Make execute_code the last one
                tools.sort(
                    key=lambda t: (
                        1 if t["function"]["name"] == "execute_code" else 0,
                        t["function"]["name"],
                    )
                )

                tool_names = [t["function"]["name"] for t in tools]
                QgsMessageLog.logMessage(
                    f"Tools list (Filtered & Prioritized): {tool_names}",
                    LOG_TAG,
                    Qgis.Info,
                )

            # Initial request
            if self.is_running:
                self._chat_loop(tools)

            self.finished.emit()
        except Exception as e:
            self.error.emit(str(e))
            self.finished.emit()  # Ensure finished is emitted even on error

    def stop(self):
        self.is_running = False

    @staticmethod
    def _extract_file_path(text):
        if not text:
            return None
        match = re.search(r'(/[^\s"“”]+\.[A-Za-z0-9]+)', text)
        if not match:
            return None
        return match.group(1).strip("“”\"'")

    @staticmethod
    def _convert_tools_to_ollama(mcp_tools):
        def simplify_schema(schema):
            if not isinstance(schema, dict):
                return schema

            # Remove title
            schema.pop("title", None)
            # Drop None defaults which are invalid for most scalar types
            if "default" in schema and schema["default"] is None:
                schema.pop("default", None)

            # Normalize type arrays like ["string", "null"] to the first non-null type
            if isinstance(schema.get("type"), list):
                non_null_types = [t for t in schema["type"] if t != "null"]
                if non_null_types:
                    schema["type"] = non_null_types[0]
                else:
                    schema.pop("type", None)

            # Handle anyOf (take first non-null type)
            if "anyOf" in schema:
                options = schema.pop("anyOf")
                for opt in options:
                    if opt.get("type") != "null":
                        schema.update(opt)
                        break

            # If properties exist but type is missing, default to object
            if "properties" in schema and "type" not in schema:
                schema["type"] = "object"

            # Recurse into properties
            if "properties" in schema:
                for prop in schema["properties"].values():
                    simplify_schema(prop)

            # Recurse into items
            if "items" in schema:
                simplify_schema(schema["items"])

            return schema

        ollama_tools = []
        for tool in mcp_tools:
            ollama_tool = {
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": simplify_schema(tool.get("inputSchema", {})),
                },
            }
            ollama_tools.append(ollama_tool)
        return ollama_tools

    def _chat_loop(self, tools):
        import uuid

        while self.is_running:
            url = "http://localhost:11434/api/chat"
            full_content = ""
            collected_tool_calls = []

            try:
                # 1. Prepare a CLEAN payload for Ollama
                clean_messages = []
                for m in self.messages:
                    # Ensure content is always a string for safety
                    msg_obj = {"role": m["role"], "content": str(m.get("content", ""))}
                    if "tool_calls" in m:
                        clean_tool_calls = []
                        for tc in m["tool_calls"]:
                            # Ollama expects arguments as an object, not a JSON string
                            args = tc["function"].get("arguments", {})
                            if isinstance(args, str):
                                try:
                                    parsed_args = json.loads(args)
                                    args = (
                                        parsed_args
                                        if isinstance(parsed_args, dict)
                                        else {}
                                    )
                                except Exception:
                                    args = {}
                            elif not isinstance(args, dict):
                                args = {}

                            cleaned_tc = {
                                "type": tc.get("type", "function"),
                                "function": {
                                    "name": tc["function"]["name"],
                                    "arguments": args,
                                },
                            }
                            # Ensure id is present
                            if "id" in tc:
                                cleaned_tc["id"] = tc["id"]
                            clean_tool_calls.append(cleaned_tc)
                        msg_obj["tool_calls"] = clean_tool_calls

                    if "tool_call_id" in m:
                        msg_obj["tool_call_id"] = m["tool_call_id"]
                    # Ollama tool messages only require tool_call_id and content
                    # Including name can trigger a bad request depending on server version
                    clean_messages.append(msg_obj)

                payload = {
                    "model": self.model,
                    "messages": clean_messages,
                    "stream": True,  # Enable streaming
                    "tools": tools,
                }

                # DEBUG: Log the full messages payload
                try:
                    msgs_preview = json.dumps(clean_messages, ensure_ascii=False)
                    QgsMessageLog.logMessage(
                        f"DEBUG: Messages Payload: {msgs_preview}", LOG_TAG, Qgis.Info
                    )
                except:
                    pass

                headers = {"Content-Type": "application/json; charset=utf-8"}

                QgsMessageLog.logMessage(
                    f"Sending request to Ollama: {len(clean_messages)} msgs, {len(tools)} tools",
                    LOG_TAG,
                    Qgis.Info,
                )

                with requests.post(
                    url, json=payload, headers=headers, stream=True, timeout=300
                ) as response:
                    if response.status_code != 200:
                        error_detail = response.text
                        QgsMessageLog.logMessage(
                            f"Ollama Error {response.status_code}: {error_detail}",
                            LOG_TAG,
                            Qgis.Critical,
                        )
                        # Log a small preview of the payload to aid debugging malformed JSON
                        try:
                            payload_preview = json.dumps(payload, ensure_ascii=False)[
                                :2000
                            ]
                            QgsMessageLog.logMessage(
                                f"Ollama payload preview: {payload_preview}",
                                LOG_TAG,
                                Qgis.Warning,
                            )
                        except Exception:
                            pass
                        response.raise_for_status()

                    tool_calls_map = {}
                    saw_content = False
                    is_thinking = False

                    for line in response.iter_lines(decode_unicode=True):
                        if not self.is_running:
                            break
                        if not line:
                            continue
                        try:
                            raw_line = line
                            if isinstance(raw_line, bytes):
                                raw_line = raw_line.decode("utf-8", "replace")
                            if raw_line.startswith("data:"):
                                raw_line = raw_line[5:].strip()
                                if not raw_line or raw_line == "[DONE]":
                                    continue
                            data = json.loads(raw_line)
                            if "error" in data:
                                QgsMessageLog.logMessage(
                                    f"Ollama stream error: {data.get('error')}",
                                    LOG_TAG,
                                    Qgis.Warning,
                                )
                            msg = data.get("message", {})

                            # Content
                            chunk = msg.get("content", "")
                            # Fallback for /api/generate style responses
                            if not chunk and "response" in data:
                                chunk = data.get("response", "")

                            if chunk:
                                # Logic to filter out <think> blocks
                                if "<think>" in chunk:
                                    is_thinking = True
                                    # Check if </think> is in the same chunk
                                    if "</think>" in chunk:
                                        parts = chunk.split("</think>", 1)
                                        # Everything before </think> is reasoning, so discard
                                        content_after = parts[1]
                                        is_thinking = False  # Reset thinking state
                                        if content_after.strip():
                                            saw_content = True
                                            self.chunk_received.emit(content_after)
                                            full_content += content_after
                                    continue  # Skip the rest of this chunk as it's either thinking or already processed

                                if "</think>" in chunk:
                                    is_thinking = False  # Reset thinking state
                                    parts = chunk.split("</think>", 1)
                                    content_after = parts[1]
                                    if content_after.strip():
                                        saw_content = True
                                        self.chunk_received.emit(content_after)
                                        full_content += content_after
                                    continue  # Skip the rest of this chunk as it's either thinking or already processed

                                if not is_thinking:
                                    saw_content = True
                                    self.chunk_received.emit(chunk)
                                    full_content += chunk

                            # Tool Calls
                            if "tool_calls" in msg:
                                for tc in msg["tool_calls"]:
                                    idx = tc.get("index", 0)
                                    if idx not in tool_calls_map:
                                        tool_calls_map[idx] = tc
                                    else:
                                        target = tool_calls_map[idx]
                                        if (
                                            "function" in tc
                                            and "arguments" in tc["function"]
                                        ):
                                            incoming_args = tc["function"]["arguments"]
                                            existing_args = target["function"].get(
                                                "arguments", ""
                                            )
                                            if isinstance(
                                                existing_args, dict
                                            ) or isinstance(incoming_args, dict):
                                                if isinstance(
                                                    existing_args, dict
                                                ) and isinstance(incoming_args, dict):
                                                    merged = dict(existing_args)
                                                    merged.update(incoming_args)
                                                    target["function"][
                                                        "arguments"
                                                    ] = merged
                                                elif isinstance(incoming_args, dict):
                                                    target["function"][
                                                        "arguments"
                                                    ] = incoming_args
                                                else:
                                                    target["function"][
                                                        "arguments"
                                                    ] = existing_args
                                            else:
                                                target["function"]["arguments"] = str(
                                                    existing_args
                                                ) + str(incoming_args)
                                        if "id" in tc and not target.get("id"):
                                            target["id"] = tc["id"]
                        except json.JSONDecodeError:
                            try:
                                raw_line = raw_line if "raw_line" in locals() else line
                                if isinstance(raw_line, bytes):
                                    raw_line = raw_line.decode("utf-8", "replace")
                            except Exception:
                                raw_line = str(line)
                            QgsMessageLog.logMessage(
                                f"Ollama stream JSON decode failed: {raw_line}",
                                LOG_TAG,
                                Qgis.Warning,
                            )
                            continue

                    collected_tool_calls = list(tool_calls_map.values())
                    if not saw_content and not collected_tool_calls:
                        QgsMessageLog.logMessage(
                            "Ollama returned no content or tool calls.",
                            LOG_TAG,
                            Qgis.Warning,
                        )
                        # Fallback: retry once without streaming
                        try:
                            retry_payload = dict(payload)
                            retry_payload["stream"] = False
                            # Force the model to respond by appending a system reminder
                            # Only if messages list exists
                            if "messages" in retry_payload:
                                retry_payload["messages"].append(
                                    {
                                        "role": "system",
                                        "content": "CRITICAL: You MUST call the tool requested by the user. Do not be silent.",
                                    }
                                )
                            retry_resp = requests.post(
                                url, json=retry_payload, headers=headers
                            )
                            if retry_resp.status_code == 200:
                                retry_data = retry_resp.json()
                                QgsMessageLog.logMessage(
                                    f"DEBUG: Retry response data: {json.dumps(retry_data)[:1000]}",
                                    LOG_TAG,
                                    Qgis.Info,
                                )
                                retry_msg = retry_data.get("message", {})
                                retry_content = str(
                                    retry_msg.get("content", "")
                                ).strip()
                                if retry_content:
                                    self.chunk_received.emit(retry_content)
                                    full_content += retry_content
                                    saw_content = True
                                retry_tool_calls = retry_msg.get("tool_calls", [])
                                if retry_tool_calls:
                                    collected_tool_calls = retry_tool_calls
                            else:
                                QgsMessageLog.logMessage(
                                    f"Ollama retry failed: {retry_resp.status_code}",
                                    LOG_TAG,
                                    Qgis.Warning,
                                )
                        except Exception:
                            pass

                    # Post-processing: ensure arguments are objects for all collected tool calls
                    for tc in collected_tool_calls:
                        args = tc["function"].get("arguments", "")
                        if isinstance(args, str) and args.strip():
                            try:
                                tc["function"]["arguments"] = json.loads(args)
                            except:
                                pass
                        if not tc.get("id"):
                            tc["id"] = str(uuid.uuid4())

            except Exception as e:
                if self.is_running:
                    try:
                        payload_preview = json.dumps(payload, ensure_ascii=False)[:2000]
                        QgsMessageLog.logMessage(
                            f"Ollama request failed; payload preview: {payload_preview}",
                            LOG_TAG,
                            Qgis.Warning,
                        )
                    except Exception:
                        pass
                    raise e

            if not self.is_running:
                break

            # Save assistant message
            assistant_msg = {"role": "assistant", "content": full_content}
            if collected_tool_calls:
                assistant_msg["tool_calls"] = collected_tool_calls
            self.messages.append(assistant_msg)

            if collected_tool_calls and self.is_running:

                # Give the UI thread a moment to process the emitted chunks/text
                # before we start the heavy blocking tool execution
                import time

                time.sleep(0.1)

                for tool_call in collected_tool_calls:
                    if not self.is_running:
                        break

                    fn_name = tool_call["function"]["name"]
                    args = tool_call["function"].get("arguments", {})
                    if isinstance(args, str) and args.strip():
                        try:
                            args = json.loads(args)
                        except Exception as parse_error:
                            QgsMessageLog.logMessage(
                                f"Failed to parse tool arguments for {fn_name}: {parse_error}",
                                LOG_TAG,
                                Qgis.Warning,
                            )

                    # Log the specific tool schema being called, as requested by user
                    if tools:
                        for t in tools:
                            if t["function"]["name"] == fn_name:
                                QgsMessageLog.logMessage(
                                    f"DEBUG: Calling tool {fn_name} with schema: {json.dumps(t, indent=2)}",
                                    LOG_TAG,
                                    Qgis.Info,
                                )
                                break
                    call_id = str(tool_call["id"])

                    self.tool_status.emit(f"Executing tool: {fn_name}...")
                    QgsMessageLog.logMessage(
                        f"Executing tool: {fn_name}", LOG_TAG, Qgis.Info
                    )
                    try:
                        args_preview = json.dumps(args, ensure_ascii=False)
                    except Exception:
                        args_preview = str(args)
                    QgsMessageLog.logMessage(
                        f"Tool params for {fn_name}: {args_preview}",
                        LOG_TAG,
                        Qgis.Info,
                    )
                    try:
                        result = self.mcp_client.call_tool(fn_name, args)
                        content = (
                            result
                            if isinstance(result, str)
                            else json.dumps(result, ensure_ascii=False)
                        )
                        self.tool_status.emit(f"Tool {fn_name} completed.")
                    except Exception as e:
                        content = f"Error: {str(e)}"
                        self.tool_status.emit(f"Tool {fn_name} failed: {str(e)}")

                    self.messages.append(
                        {
                            "role": "tool",
                            "content": content,
                            "tool_call_id": call_id,
                        }
                    )
                continue
            else:
                break


class ChatDockWidget(QtWidgets.QDockWidget):
    def __init__(self, parent=None):
        super().__init__("Smart QGIS Assistant", parent)
        self.mcp_client = None
        self.server_controller = None
        self.fetch_thread = None
        self.worker = None

        # Main container widget
        container = QtWidgets.QWidget()
        self.setWidget(container)

        # Layouts
        main_layout = QtWidgets.QVBoxLayout(container)
        # Chat history panel
        self.txt_history = QtWidgets.QTextBrowser()
        self.txt_history.setReadOnly(True)
        # Apply rounded corners and styles
        self.txt_history.setStyleSheet(STYLE_ROUNDED_CORNER)
        self.txt_history.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)
        self.txt_history.setLineWrapMode(QtWidgets.QTextEdit.WidgetWidth)
        # User input field
        self.txt_input = QtWidgets.QTextEdit()
        self.txt_input.setPlaceholderText("Ask me something...")
        self.txt_input.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding
        )
        self.txt_input.setStyleSheet(STYLE_ROUNDED_CORNER)

        splitter = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self.txt_history)
        splitter.addWidget(self.txt_input)
        splitter.setStretchFactor(0, 7)
        splitter.setStretchFactor(1, 3)
        main_layout.addWidget(splitter)

        # Bottom button panel
        footer = QtWidgets.QWidget()
        footer_layout = QtWidgets.QHBoxLayout(footer)
        footer_layout.setContentsMargins(2, 2, 2, 2)
        self.combo_models = QtWidgets.QComboBox()
        self.combo_models.setStyleSheet(get_combobox_style())
        self.populate_models()
        footer_layout.addWidget(self.combo_models, alignment=QtCore.Qt.AlignLeft)

        self.btn_stop = QtWidgets.QPushButton("Stop")
        self.btn_stop.setStyleSheet(STYLE_STOP_BUTTON)
        self.btn_stop.setEnabled(False)  # Disabled by default
        footer_layout.addWidget(self.btn_stop, alignment=QtCore.Qt.AlignCenter)

        self.btn_clear = QtWidgets.QPushButton("Clear")
        self.btn_clear.setStyleSheet(STYLE_BUTTON)
        footer_layout.addWidget(self.btn_clear, alignment=QtCore.Qt.AlignCenter)

        # Send button on the right
        self.btn_send = QtWidgets.QPushButton("Send")
        send_icon = Path(__file__).parent / "resources" / "send.svg"
        self.btn_send.setIcon(QIcon(str(send_icon)))
        self.btn_send.setFixedHeight(30)
        self.btn_send.setStyleSheet(STYLE_BUTTON)
        footer_layout.addWidget(self.btn_send, alignment=QtCore.Qt.AlignRight)

        main_layout.addWidget(footer)

        # Server Control Panel
        server_control_group = QtWidgets.QWidget()
        server_layout = QtWidgets.QHBoxLayout(server_control_group)
        server_layout.setContentsMargins(5, 5, 5, 5)

        self.btn_socket_toggle = QtWidgets.QPushButton("Socket: ON")
        self.btn_socket_toggle.setStyleSheet(STYLE_SERVER_BTN_ON)
        self.btn_mcp_toggle = QtWidgets.QPushButton("MCP: ON")
        self.btn_mcp_toggle.setStyleSheet(STYLE_SERVER_BTN_ON)

        server_layout.addWidget(self.btn_socket_toggle)
        server_layout.addWidget(self.btn_mcp_toggle)

        main_layout.addWidget(server_control_group)
        # Bind events
        self.btn_send.clicked.connect(self.handle_send)
        self.btn_stop.clicked.connect(self.handle_stop)
        self.btn_clear.clicked.connect(self.handle_clear)
        self.btn_socket_toggle.clicked.connect(self.toggle_socket_server)
        self.btn_mcp_toggle.clicked.connect(self.toggle_mcp_server)

        # Initialize thread attribute
        self.thread = None
        # Set initial messages including system prompt and initial AI greeting
        self.messages = [
            {
                "role": "system",
                "content": QGIS_AGENT_SYSTEM_PROMPT,
            }
        ]  # Keep track of conversation history
        # Add initial AI greeting to chat history display
        initial_greeting = (
            '<div style="margin: 10px 0">'
            '<span style="background-color:#90EE90; border-radius:10px; padding:10px; display:inline-block;">'
            "<strong>AI Agent:</strong><br/>Hello! I am the Smart QGIS Agent. I can help you with spatial analysis, data processing, and mapping tasks in QGIS. I can also interact with QGIS using various tools. How can I help you today?</span></div>"
        )
        self.txt_history.append(initial_greeting)

        self.setObjectName("Smart QGIS Dock")

    def set_server_controller(self, controller):
        self.server_controller = controller
        self.update_server_status_ui()

    def on_mcp_server_changed(self, client):
        self.mcp_client = client
        self.update_server_status_ui()
        if client:
            QgsMessageLog.logMessage("MCP Server connected.", LOG_TAG, Qgis.Info)
        else:
            QgsMessageLog.logMessage("MCP Server disconnected.", LOG_TAG, Qgis.Info)

    def update_server_status_ui(self):
        if not self.server_controller:
            return

        # Update Socket Button
        if self.server_controller.is_socket_server_running():
            self.btn_socket_toggle.setText("Socket: ON")
            self.btn_socket_toggle.setStyleSheet(STYLE_SERVER_BTN_ON)
        else:
            self.btn_socket_toggle.setText("Socket: OFF")
            self.btn_socket_toggle.setStyleSheet(STYLE_SERVER_BTN_OFF)

        # Update MCP Button
        if self.server_controller.is_mcp_server_running():
            self.btn_mcp_toggle.setText("MCP: ON")
            self.btn_mcp_toggle.setStyleSheet(STYLE_SERVER_BTN_ON)
        else:
            self.btn_mcp_toggle.setText("MCP: OFF")
            self.btn_mcp_toggle.setStyleSheet(STYLE_SERVER_BTN_OFF)

    def toggle_socket_server(self):
        if not self.server_controller:
            return

        if self.server_controller.is_socket_server_running():
            self.server_controller.stop_socket_server()
        else:
            self.server_controller.start_socket_server()
        self.update_server_status_ui()

    def toggle_mcp_server(self):
        if not self.server_controller:
            return

        if self.server_controller.is_mcp_server_running():
            self.server_controller.stop_mcp_server()
        else:
            self.server_controller.start_mcp_server()
        self.update_server_status_ui()

    def closeEvent(self, event):
        if self.thread and self.thread.isRunning():
            self.thread.quit()
            self.thread.wait()
        super().closeEvent(event)

    def populate_models(self):
        self.combo_models.clear()
        self.combo_models.addItem("Loading...")

        self.fetch_thread = ModelFetcherThread()
        self.fetch_thread.models_fetched.connect(self.on_models_fetched)
        self.fetch_thread.error_occurred.connect(self.on_models_error)
        self.fetch_thread.finished.connect(self.fetch_thread.deleteLater)
        self.fetch_thread.start()

    def on_models_fetched(self, models):
        self.combo_models.clear()
        if models:
            self.combo_models.addItems(models)
        else:
            self.combo_models.addItem("No models found")

    def on_models_error(self, error_msg):
        self.combo_models.clear()
        self.combo_models.addItem("Cannot load Ollama models")
        QgsMessageLog.logMessage(
            f"Error fetching models: {error_msg}", LOG_TAG, Qgis.Critical
        )

    @pyqtSlot()
    def handle_stop(self):
        if self.worker:
            self.worker.stop()
            # self.txt_history.append("<i>Stopped by user.</i>")
            self.btn_stop.setEnabled(False)
            self.btn_send.setEnabled(True)  # Re-enable send button
            self.txt_input.setFocus()

    @pyqtSlot()
    def handle_clear(self):
        """Reset the conversation history and clear UI."""
        self.txt_history.clear()
        # Re-initialize system prompt
        self.messages = [
            {
                "role": "system",
                "content": QGIS_AGENT_SYSTEM_PROMPT,
            }
        ]

        # Re-add greeting
        initial_greeting = (
            '<div style="margin: 10px 0">'
            '<span style="background-color:#90EE90; border-radius:10px; padding:10px; display:inline-block;">'
            "<strong>AI Agent:</strong><br/>Hello! History cleared. How can I help you?</span></div>"
        )
        self.txt_history.append(initial_greeting)

        self.txt_input.clear()
        self.txt_input.setFocus()

    def on_chat_finished(self):
        self.btn_send.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.txt_input.setFocus()

    @pyqtSlot()
    def handle_send(self):
        text = self.txt_input.toPlainText().strip()
        if not text:
            return

        model = self.combo_models.currentText()
        if not model or model in [
            "Loading...",
            "Cannot load Ollama models",
            "No models found",
        ]:
            QgsMessageLog.logMessage(
                f"Invalid model selected: {model}", LOG_TAG, Qgis.Critical
            )
            self.txt_history.append("<i>Error: Please select a valid model.</i>")
            return

        # Disable send, enable stop
        self.btn_send.setEnabled(False)
        self.btn_stop.setEnabled(True)

        # Display user message
        user_html = (
            f'<div style="margin: 10px 0">'
            f'<span style="background-color:#90D5FF; border-radius:10px; padding:10px; display:inline-block;">'
            f"<strong>User:</strong><br/>{text}</span></div>"
        )
        self.txt_history.append(user_html)
        self.txt_input.clear()
        # Prepare AI response area
        ai_html = '<div style="margin: 10px 0"><strong>AI Agent: </strong><br/></div>'
        self.txt_history.append(ai_html)
        self.txt_history.moveCursor(QTextCursor.End)
        # Add to history
        self.messages.append({"role": "user", "content": text})
        # Start worker
        self.thread = QtCore.QThread()
        self.worker = ChatWorker(model, self.messages, self.mcp_client)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.chunk_received.connect(self.update_ai_response)
        self.worker.finished.connect(self.thread.quit)
        self.worker.finished.connect(self.worker.deleteLater)
        self.worker.finished.connect(self.on_chat_finished)  # Connect to reset UI
        self.thread.finished.connect(self.thread.deleteLater)
        self.worker.error.connect(self.on_chat_error)
        self.thread.start()

    def update_ai_response(self, chunk):
        self.txt_history.moveCursor(QTextCursor.End)
        self.txt_history.insertPlainText(chunk)
        self.txt_history.moveCursor(QTextCursor.End)
        # Force UI update to show streaming content immediately
        QtWidgets.QApplication.processEvents()

    def on_chat_error(self, e):
        QgsMessageLog.logMessage(f"Chat error: {e}", LOG_TAG, Qgis.Critical)
        self.txt_history.append(f"<br/><span style='color:red'>Error: {str(e)}</span>")
