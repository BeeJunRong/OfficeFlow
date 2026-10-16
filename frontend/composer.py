"""A single, persistent chat composer for the welcome and conversation views."""

from pathlib import Path

import streamlit.components.v2 as components

_ASSETS = Path(__file__).with_name("composer_assets")
chat_composer = components.component(
    "officeflow_composer",
    html=(_ASSETS / "composer.html").read_text(encoding="utf-8"),
    css=(_ASSETS / "composer.css").read_text(encoding="utf-8"),
    js=(_ASSETS / "composer.js").read_text(encoding="utf-8"),
)
