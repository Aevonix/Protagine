"""Shared admission for OpenAI-compatible inference replicas.

This boundary schedules model calls, not agent tasks. Deployment profiles own
endpoint identities, role mappings and capacity estimates.
"""
