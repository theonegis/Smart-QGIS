from pathlib import Path

# Common Styles
STYLE_ROUNDED_CORNER = """
QWidget {
    border: 1px solid #cccccc;
    border-radius: 10px;
    padding: 5px;
    background-color: #ffffff;
}
"""

STYLE_BUTTON = """
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

STYLE_STOP_BUTTON = """
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

STYLE_SERVER_BTN_ON = """
QPushButton {
    background-color: #198754;
    border: 1px solid #198754;
    color: white;
    padding: 4px 8px;
    border-radius: 4px;
    font-size: 12px;
}
QPushButton:hover { background-color: #157347; }
"""

STYLE_SERVER_BTN_OFF = """
QPushButton {
    background-color: #6c757d;
    border: 1px solid #6c757d;
    color: white;
    padding: 4px 8px;
    border-radius: 4px;
    font-size: 12px;
}
QPushButton:hover { background-color: #5c636a; }
"""

def get_combobox_style():
    arrow_icon = Path(__file__).parent / "resources" / "down-arrow.png"
    # Convert path to string using forward slashes for CSS compatibility if needed, 
    # though usually string conversion is enough.
    return f"""
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
