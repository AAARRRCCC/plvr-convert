"""Build the two companion iOS shortcuts, unsigned, as binary plists.

    python tools/make_shortcuts.py <out dir>

Each one is three actions, the same shape as cobalt's: take the link shared
to it, download it, save it (to Photos, or wherever you pick in Files). The
page shares a /api/download link, which needs no cookies, so the shortcut
needs nothing of convert's.

iOS only imports signed shortcuts. Sign on a Mac signed in to iCloud:

    shortcuts sign --mode anyone --input "save to photos.shortcut" --output static/shortcuts/photos.shortcut
"""
from __future__ import annotations

import plistlib
import sys
import uuid
from pathlib import Path

ICON_COLOR = 255  # black
ICON_GLYPH = 61460  # the down-arrow tray, same glyph cobalt uses


def _token(output_uuid: str, name: str) -> dict:
    return {
        "Value": {
            "string": "￼",
            "attachmentsByRange": {"{0, 1}": {"OutputUUID": output_uuid, "Type": "ActionOutput", "OutputName": name}},
        },
        "WFSerializationType": "WFTextTokenString",
    }


def build(save_action: dict) -> dict:
    links, fetch = str(uuid.uuid4()).upper(), str(uuid.uuid4()).upper()
    return {
        "WFWorkflowClientVersion": "1417.1",
        "WFWorkflowMinimumClientVersion": 1113,
        "WFWorkflowMinimumClientVersionString": "1113",
        "WFWorkflowIcon": {"WFWorkflowIconStartColor": ICON_COLOR, "WFWorkflowIconGlyphNumber": ICON_GLYPH},
        # share sheet only, taking links
        "WFWorkflowTypes": ["ActionExtension"],
        "WFWorkflowInputContentItemClasses": ["WFURLContentItem"],
        "WFWorkflowHasShortcutInputVariables": True,
        "WFWorkflowNoInputBehavior": {"Name": "WFWorkflowNoInputBehaviorGetClipboard", "Parameters": {}},
        "WFWorkflowOutputContentItemClasses": [],
        "WFWorkflowHasOutputFallback": False,
        "WFWorkflowImportQuestions": [],
        "WFQuickActionSurfaces": [],
        "WFWorkflowActions": [
            {
                "WFWorkflowActionIdentifier": "is.workflow.actions.detect.link",
                "WFWorkflowActionParameters": {
                    "UUID": links,
                    "WFInput": {
                        "Value": {"string": "￼", "attachmentsByRange": {"{0, 1}": {"Type": "ExtensionInput"}}},
                        "WFSerializationType": "WFTextTokenString",
                    },
                },
            },
            {
                "WFWorkflowActionIdentifier": "is.workflow.actions.downloadurl",
                "WFWorkflowActionParameters": {"UUID": fetch, "WFURL": _token(links, "URLs"), "ShowHeaders": True},
            },
            {
                "WFWorkflowActionIdentifier": save_action["id"],
                "WFWorkflowActionParameters": {
                    "UUID": str(uuid.uuid4()).upper(),
                    "WFInput": {
                        "Value": {"OutputUUID": fetch, "Type": "ActionOutput", "OutputName": "Contents of URL"},
                        "WFSerializationType": "WFTextTokenAttachment",
                    },
                    **save_action.get("params", {}),
                },
            },
        ],
    }


SHORTCUTS = {
    "save to photos.shortcut": {"id": "is.workflow.actions.savetocameraroll"},
    "save to files.shortcut": {"id": "is.workflow.actions.documentpicker.save"},
}


def main() -> None:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    out.mkdir(parents=True, exist_ok=True)
    for name, action in SHORTCUTS.items():
        (out / name).write_bytes(plistlib.dumps(build(action), fmt=plistlib.FMT_BINARY))
        print(out / name)


if __name__ == "__main__":
    main()
