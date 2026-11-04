"""Panels of «Учёба» (D-039, docs/v4/23-panels-backend.md): declarative views over the domain data.

A panel is a thin config (`db.models.Panel`): a type from a closed registry, a validated `spec`
and the student's switches. The data stays in the domain tables with provenance; the API resolves
it, bounded, on every read.
"""
