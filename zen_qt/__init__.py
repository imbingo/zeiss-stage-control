"""PySide6 front end for the ZEN surface-tilt workbench.

The Qt layer is a thin shell over the existing backend (zen_workflow,
zen_hardware, zen_marks, zen_records): it owns no device state of its own
and only renders what the workflow reports.
"""
