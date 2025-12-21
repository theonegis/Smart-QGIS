import json
import logging
import threading
import time
import sys
from concurrent.futures import Future
from qgis.core import QgsMessageLog, Qgis

LOG_TAG = "Smart QGIS"

class McpClient:
    def __init__(self, process):
        QgsMessageLog.logMessage(f"MCP Client running on Python: {sys.executable}", LOG_TAG, Qgis.Info)
        self.process = process
        self.request_id = 0
        self.lock = threading.Lock()
        self.pending_requests = {}
        
        # Start reader thread
        self.stdout_thread = threading.Thread(target=self.read_stdout_loop, daemon=True)
        self.stdout_thread.start()
        # Initialize
        self.initialize()

    def read_stdout_loop(self):
        while True:
            if self.process.poll() is not None:
                QgsMessageLog.logMessage("MCP server process ended", LOG_TAG, Qgis.Info)
                break
            
            try:
                line = self.process.stdout.readline()
                if not line:
                    continue
                
                # Try to parse as JSON-RPC response
                try:
                    response = json.loads(line)
                    if "id" in response:
                        # It's a JSON-RPC response
                        QgsMessageLog.logMessage(f"MCP Response: {line.strip()}", LOG_TAG, Qgis.Info)
                        req_id = response["id"]
                        with self.lock:
                            if req_id in self.pending_requests:
                                future = self.pending_requests.pop(req_id)
                                if "error" in response:
                                    future.set_exception(Exception(response["error"]["message"]))
                                else:
                                    future.set_result(response.get("result"))
                    else:
                        # It's a JSON message but not a response we are waiting for (e.g. notification)
                        QgsMessageLog.logMessage(f"MCP Notification: {line.strip()}", LOG_TAG, Qgis.Info)
                except json.JSONDecodeError:
                    # Not JSON, treat as log output (stderr merged here)
                    QgsMessageLog.logMessage(f"MCP Log: {line.strip()}", LOG_TAG, Qgis.Info)
                    
            except Exception as e:
                QgsMessageLog.logMessage(f"Error reading from MCP server stdout: {e}", LOG_TAG, Qgis.Critical)
                break

    def send_request(self, method, params=None, timeout=10):
        future = Future()
        with self.lock:
            self.request_id += 1
            req_id = self.request_id
            self.pending_requests[req_id] = future
        
        request = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params or {}
        }
        
        json_req = json.dumps(request) + "\n"
        QgsMessageLog.logMessage(f"Sending MCP request: {json_req.strip()}", LOG_TAG, Qgis.Info)
        self.process.stdin.write(json_req)
        self.process.stdin.flush()
        
        # Wait for response using Future
        try:
            return future.result(timeout=timeout)
        except Exception as e:
            QgsMessageLog.logMessage(f"MCP request failed or timed out: {e}", LOG_TAG, Qgis.Critical)
            with self.lock:
                if req_id in self.pending_requests:
                    del self.pending_requests[req_id]
            raise e

    def initialize(self):
        return self.send_request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "QGIS Plugin", "version": "0.1"}
        })

    def list_tools(self):
        response = self.send_request("tools/list")
        return response.get("tools", [])

    def call_tool(self, name, arguments, timeout=600):
        # Default timeout for tools is 10 minutes (600s) to allow for long running operations
        return self.send_request("tools/call", {
            "name": name,
            "arguments": arguments
        }, timeout=timeout)
