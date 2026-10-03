"""Readable, unabridged terminal tracing; machine-readable data stays in JSONL."""
import json


def display_trace(emit, label, data):
    emit(f"\n--- {label} ---")
    # Show message text literally, so Windows paths and multiline prompts remain
    # readable rather than appearing as JSON-escaped backslashes and newlines.
    def display(value, prefix=""):
        if isinstance(value, dict):
            for key, item in value.items():
                if isinstance(item, (dict, list)):
                    emit(f"{prefix}{key}:")
                    display(item, prefix + "  ")
                elif isinstance(item, str):
                    emit(f"{prefix}{key}:")
                    emit("\n".join(prefix + "  " + line for line in item.split("\n")))
                else:
                    emit(f"{prefix}{key}: {json.dumps(item)}")
        elif isinstance(value, list):
            if not value:
                emit(prefix + "[]")
            for index, item in enumerate(value):
                emit(f"{prefix}[{index}]")
                display(item, prefix + "  ")
        else:
            emit(prefix + str(value))
    display(data)
    emit(f"--- END {label} ---\n")
