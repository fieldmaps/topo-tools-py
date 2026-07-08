"""Detects and fixes coverage defects (gaps, overlaps) in a single polygon layer.

Slivers (near-miss boundary mismatches) are detected and reported but never
auto-fixed -- see _02_issues.py and docs/cleaning.md.
"""
