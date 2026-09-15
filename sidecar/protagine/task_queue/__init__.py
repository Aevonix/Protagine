"""Protagine Distributed Task Queue.

Hardware-aware distributed job scheduling across the Protagine mesh.
The Queen (Sovereign) schedules; Workers (Vassals) execute.

Public API::

    from protagine.task_queue import (
        Job, JobType, JobStatus, JobPriority,
        JobCapabilityRequirement, JobResult,
        WorkerCapabilities, QueueManager, WorkerNode,
        JobHandler, Scheduler, TaskQueueConfig,
    )
"""

from protagine.task_queue.config import TaskQueueConfig
from protagine.task_queue.mesh_integration import QueueMeshEventHandler
from protagine.task_queue.models import (
    AuditEntry,
    CircularDependencyError,
    FederatedJob,
    HeartbeatPayload,
    Job,
    JobCapabilityRequirement,
    JobPriority,
    JobResult,
    JobStatus,
    JobType,
    QueueStats,
    WorkerCapabilities,
    deadline_urgency,
)
from protagine.task_queue.queue_manager import QueueManager
from protagine.task_queue.scheduler import Scheduler
from protagine.task_queue.worker import JobHandler, WorkerNode, detect_local_capabilities
from protagine.task_queue.work_control import (
    WorkControlError,
    WorkControlService,
)

__all__ = [
    # Models
    "Job",
    "JobType",
    "JobStatus",
    "JobPriority",
    "JobCapabilityRequirement",
    "JobResult",
    "WorkerCapabilities",
    "HeartbeatPayload",
    "QueueStats",
    "AuditEntry",
    "FederatedJob",
    "CircularDependencyError",
    "deadline_urgency",
    # Core components
    "QueueManager",
    "WorkControlService",
    "WorkControlError",
    "WorkerNode",
    "JobHandler",
    "Scheduler",
    # Config
    "TaskQueueConfig",
    # Mesh integration
    "QueueMeshEventHandler",
    # Utilities
    "detect_local_capabilities",
]
