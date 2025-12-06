from qgis.PyQt import QtWidgets
from qgis.PyQt import QtCore
from qgis.PyQt.QtCore import pyqtSlot
from qgis.PyQt.QtGui import QIcon, QTextCursor
from qgis.core import QgsMessageLog, Qgis
from pathlib import Path
import requests
import json

LOG_TAG = "QGIS AI"


class ModelFetcherThread(QtCore.QThread):
    """Thread to fetch models from Ollama."""
    models_fetched = QtCore.pyqtSignal(list)
    error_occurred = QtCore.pyqtSignal(str)

    def run(self):
        QgsMessageLog.logMessage("ModelFetcherThread: Starting run()", LOG_TAG, Qgis.Info)
        try:
            response = requests.get("http://localhost:11434/api/tags")
            QgsMessageLog.logMessage(f"ModelFetcherThread: Response status {response.status_code}", LOG_TAG, Qgis.Info)
            if response.status_code == 200:
                data = response.json()
                models = [model['name'] for model in data.get('models', [])]
                QgsMessageLog.logMessage(f"ModelFetcherThread: Models found {models}", LOG_TAG, Qgis.Info)
                self.models_fetched.emit(models)
            else:
                raise Exception(f"Status code: {response.status_code}")
        except Exception as e:
            QgsMessageLog.logMessage(f"ModelFetcherThread: Error {e}", LOG_TAG, Qgis.Critical)
            self.error_occurred.emit(str(e))


class ChatWorker(QtCore.QObject):
    """Worker to handle chat interaction with Ollama, including tool calling."""
    chunk_received = QtCore.pyqtSignal(str)
    finished = QtCore.pyqtSignal()
    error = QtCore.pyqtSignal(str)

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
                    QgsMessageLog.logMessage(f"Failed to fetch tools: {e}", LOG_TAG, Qgis.Warning)

            # Initial request
            if self.is_running:
                self._chat_loop(tools)

            self.finished.emit()
        except Exception as e:
            self.error.emit(str(e))
            self.finished.emit() # Ensure finished is emitted even on error

    def stop(self):
        self.is_running = False

    @staticmethod
    def _convert_tools_to_ollama(mcp_tools):
        ollama_tools = []
        for tool in mcp_tools:
            ollama_tool = {
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": tool.get("inputSchema", {})
                }
            }
            ollama_tools.append(ollama_tool)
        return ollama_tools

    def _chat_loop(self, tools):
        while self.is_running:
            payload = {
                "model": self.model,
                "messages": self.messages,
                "stream": True
            }
            if tools:
                payload["tools"] = tools

            url = "http://localhost:11434/api/chat"
            full_content = ""
            collected_tool_calls = []

            # Log the request payload for debugging
            QgsMessageLog.logMessage(f"Sending request to Ollama with model: {self.model}", LOG_TAG, Qgis.Info)
            QgsMessageLog.logMessage(f"Number of messages: {len(self.messages)}", LOG_TAG, Qgis.Info)
            QgsMessageLog.logMessage(f"Number of tools: {len(tools)}", LOG_TAG, Qgis.Info)

            try:
                with requests.post(url, json=payload, stream=True) as response:
                    if response.status_code != 200:
                        error_detail = response.text
                        QgsMessageLog.logMessage(f"Ollama API Error: {response.status_code} - {error_detail}", LOG_TAG, Qgis.Critical)
                    response.raise_for_status()
                    for line in response.iter_lines():
                        if not self.is_running:
                            break
                        if not line: continue
                        try:
                            data = json.loads(line)
                            chunk = data.get("message", {}).get("content", "")
                            if chunk:
                                self.chunk_received.emit(chunk)
                                full_content += chunk

                            if "tool_calls" in data.get("message", {}):
                                calls = data["message"]["tool_calls"]
                                if calls:
                                    collected_tool_calls.extend(calls)

                        except json.JSONDecodeError:
                            continue
            except Exception as e:
                # If stopped, we might get connection errors, ignore if not running
                if self.is_running:
                    raise e

            if not self.is_running:
                break

            # Append the full assistant message to history
            assistant_msg = {
                "role": "assistant",
                "content": full_content
            }
            if collected_tool_calls:
                assistant_msg["tool_calls"] = collected_tool_calls

            self.messages.append(assistant_msg)

            if collected_tool_calls and self.is_running:
                QgsMessageLog.logMessage(f"Tool calls requested: {len(collected_tool_calls)}", LOG_TAG, Qgis.Info)
                for tool_call in collected_tool_calls:
                    if not self.is_running:
                        break
                    function_name = tool_call["function"]["name"]
                    arguments = tool_call["function"]["arguments"]

                    QgsMessageLog.logMessage(f"Calling tool: {function_name} with {arguments}", LOG_TAG, Qgis.Info)

                    try:
                        result = self.mcp_client.call_tool(function_name, arguments)
                        content = json.dumps(result)
                    except Exception as e:
                        content = f"Error calling tool: {str(e)}"

                    self.messages.append({
                        "role": "tool",
                        "content": content,
                    })
                # Loop back to send tool results
                continue
            else:
                # No tool calls, we are done
                break


class ChatDockWidget(QtWidgets.QDockWidget):
    def __init__(self, mcp_client, parent=None):
        super().__init__("QGIS AI Assistant", parent)
        self.mcp_client = mcp_client
        self.fetch_thread = None
        self.worker = None
        style_rounded_corner = """
        QWidget {
            border: 1px solid #cccccc;
            border-radius: 10px;
            padding: 5px;
            background-color: #ffffff;
        }
        """
        style_button = """
        QPushButton {
            background-color: #0d6efd;
            border: 1px solid #0d6efd;
            color: white;
            padding: 6px 12px;
            border-radius: 4px;
            font-size: 14px;
            font-weight: 400;
        }
        
        QPushButton:hover {
            background-color: #0b5ed7;
            border-color: #0a58ca;
        }

        QPushButton:pressed {
            background-color: #0a58ca;
            border-color: #0a53be;
        }
        
        QPushButton:disabled {
            background-color: #6c757d;
            border-color: #6c757d;
        }
        """
        
        style_stop_button = """
        QPushButton {
            background-color: #dc3545;
            border: 1px solid #dc3545;
            color: white;
            padding: 6px 12px;
            border-radius: 4px;
            font-size: 14px;
            font-weight: 400;
        }
        
        QPushButton:hover {
            background-color: #bb2d3b;
            border-color: #b02a37;
        }

        QPushButton:pressed {
            background-color: #b02a37;
            border-color: #a52834;
        }
        
        QPushButton:disabled {
            background-color: #e9ecef;
            border-color: #dee2e6;
            color: #adb5bd;
        }
        """

        arrow_icon = Path(__file__).parent / "resources" / "down-arrow.png"
        style_combobox = f"""
        QComboBox {{
            border: 1px solid #ced4da;
            border-radius: 4px;
            padding: 6px 12px;
            min-width: 150px;
            background-color: #ffffff;
            color: #495057;
            font-size: 14px;
        }}
        QComboBox:hover {{
            border-color: #b3b7bb;
        }}
        QComboBox:on {{ /* shift the text when the popup opens */
            border-color: #86b7fe;
            outline: 0;
        }}
        QComboBox::drop-down {{
            subcontrol-origin: padding;
            subcontrol-position: top right;
            width: 30px; /* Wider to accommodate icon */
            border-left-width: 0px;
            border-top-right-radius: 4px;
            border-bottom-right-radius: 4px;
        }}
        QComboBox::down-arrow {{
            image: url({arrow_icon});
            width: 9px;
            height: 9px;
            margin-right: 10px;
        }}
        """

        # Main container widget
        container = QtWidgets.QWidget()
        self.setWidget(container)
        # Layouts
        main_layout = QtWidgets.QVBoxLayout(container)
        # Chat history panel
        self.txt_history = QtWidgets.QTextBrowser()
        self.txt_history.setReadOnly(True)
        # Apply rounded corners and styles
        self.txt_history.setStyleSheet(style_rounded_corner)
        self.txt_history.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)
        self.txt_history.setLineWrapMode(QtWidgets.QTextEdit.WidgetWidth)
        # User input field
        self.txt_input = QtWidgets.QTextEdit()
        self.txt_input.setPlaceholderText("Ask me something...")
        self.txt_input.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding)
        self.txt_input.setStyleSheet(style_rounded_corner)

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
        # Model selection dropdown
        self.combo_models = QtWidgets.QComboBox()
        self.combo_models.setStyleSheet(style_combobox)
        self.populate_models()
        footer_layout.addWidget(self.combo_models, alignment=QtCore.Qt.AlignLeft)
        
        # Stop button in the center
        self.btn_stop = QtWidgets.QPushButton("Stop")
        self.btn_stop.setStyleSheet(style_stop_button)
        self.btn_stop.setEnabled(False) # Disabled by default
        footer_layout.addWidget(self.btn_stop, alignment=QtCore.Qt.AlignCenter)

        # Send button on the right
        self.btn_send = QtWidgets.QPushButton("Send")
        send_icon = Path(__file__).parent / "resources" / "send.svg"
        self.btn_send.setIcon(QIcon(str(send_icon)))
        self.btn_send.setFixedHeight(30)
        self.btn_send.setStyleSheet(style_button)
        footer_layout.addWidget(self.btn_send, alignment=QtCore.Qt.AlignRight)

        main_layout.addWidget(footer)
        # Bind events
        self.btn_send.clicked.connect(self.handle_send)
        self.btn_stop.clicked.connect(self.handle_stop)
        
        # Initialize thread attribute
        self.thread = None
        # Set initial messages including system prompt and initial AI greeting
        self.messages = [
            {
                "role": "system",
                "content": (
                    "你是一个地理信息与遥感专家，作为精通QGIS人工智能助手，你能够协助用户完成各类空间数据处理、分析与制图的相关任务，并可调用多种工具与 QGIS应用程序进行交互.\n"
                    "IMPORTANT: 若用户请求中缺失特定参数（如图层名称、文件路径或字段名称），请尝试从上下文信息中推导补全.\n"
                    "- 若图层名称缺失，核查是否存在近期提及或添加的图层，默认可以使用当前活动图层；对于输出结果图层，若无相关信息，则采用合理默认值（例如'结果图层'）.\n"
                    "- 若文件路径缺失，核查是否存在近期提及或添加的文件路径，若无相关信息，则采用合理默认值（例如用户主目录下的Desktop目录或临时位置）.文件名称若没有指定，则可根据操作名称或输入数据名称进行合理推导.\n"
                    "- 若需使用筛选表达式但用户未完整指定，可根据用户意图构建有效的 QGIS表达式（例如\"name\"='Value'）."
                )
            }
        ]  # Keep track of conversation history
        # Add initial AI greeting to chat history display
        initial_greeting = (
            '<div style="margin: 10px 0">'
            '<span style="background-color:#90EE90; border-radius:10px; padding:10px; display:inline-block;">'
            '<strong>AI智能体:</strong><br/>你好！我是QGIS AI智能体，很高兴与你交流。我能够帮助你完成各类空间数据处理、分析与制图的相关任务，并可调用多种工具与QGIS应用程序进行交互。有什么问题，我将竭诚为你服务。</span></div>'
        )
        self.txt_history.append(initial_greeting)
        self.setObjectName("QGIS AI Assistant Dock")
        
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
        QgsMessageLog.logMessage(f"Error fetching models: {error_msg}", LOG_TAG, Qgis.Critical)

    @pyqtSlot()
    def handle_stop(self):
        if self.worker:
            self.worker.stop()
            self.txt_history.append("<i>Stopped by user.</i>")
            self.btn_stop.setEnabled(False)

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
        if not model or model in ["Loading...", "Cannot load Ollama models", "No models found"]:
            QgsMessageLog.logMessage(f"Invalid model selected: {model}", LOG_TAG, Qgis.Critical)
            self.txt_history.append("<i>Error: Please select a valid model.</i>")
            return

        # Disable send, enable stop
        self.btn_send.setEnabled(False)
        self.btn_stop.setEnabled(True)

        # Display user message
        user_html = (
            f'<div style="margin: 10px 0">'
            f'<span style="background-color:#90D5FF; border-radius:10px; padding:10px; display:inline-block;">'
            f'<strong>用户:</strong><br/>{text}</span></div>'
        )
        self.txt_history.append(user_html)
        self.txt_input.clear()
        # Prepare AI response area
        ai_html = (
            f'<div style="margin: 10px 0">'
            f'<strong>AI智能体: </strong><br/></div>'
        )
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
        self.worker.finished.connect(self.on_chat_finished) # Connect to reset UI
        self.thread.finished.connect(self.thread.deleteLater)
        self.worker.error.connect(lambda e: QgsMessageLog.logMessage(f"Chat error: {e}", LOG_TAG, Qgis.Critical))
        self.thread.start()

    def update_ai_response(self, chunk):
        self.txt_history.moveCursor(QTextCursor.End)
        self.txt_history.insertPlainText(chunk)
        self.txt_history.moveCursor(QTextCursor.End)
        QtWidgets.QApplication.processEvents()
