# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Initial release (not yet cut on real PyPI, only rehearsed on TestPyPI): four
tools, CLI + Python API for each.

- `extend` — Voronoi-based polygon boundary extension, producing a complete
  coverage layer that fills gaps.
- `match` — fits a child polygon layer into a coarser parent/clip layer by
  largest-overlap assignment, then runs `extend`'s pipeline per group.
- `clean` — detects and fixes coverage gaps/overlaps via `ST_CoverageClean`;
  detects (but never auto-fixes) slivers, reported separately for review.
- `change` — compares two versions of a polygon layer and classifies every
  unit as unchanged/renamed/modified/relocated/split/merge/complex/created/
  removed, via spatial overlap and optional code/name identity linking.

[Unreleased]: https://github.com/fieldmaps/topo-tools-py/commits/main
