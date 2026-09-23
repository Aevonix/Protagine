"""Controlled native integration, distinct from real-model performance."""
import asyncio
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import re
import threading

import pytest

# The live transition runs of this pack exercised the plugin task platform
# (protagine_task, task handoffs) that M1 removed; the pack definitions and
# their evaluators stay until the M2/M3 cleanup replaces them.

from protagine.qualification.native import configuration, native_context
from protagine.qualification.native_unified import CONSUMERS, EVALUATORS
from protagine.qualification.native_unified_cases import CASES, cases
from protagine.qualification.records import read
from protagine.qualification.runner import evaluate


