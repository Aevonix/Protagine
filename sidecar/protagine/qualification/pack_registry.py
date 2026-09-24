"""Names and boundaries for shipped case factories, not another evaluator."""
from dataclasses import dataclass
from importlib import import_module


@dataclass(frozen=True)
class Pack:
    cases_module: str
    consumer_module: str
    mode: str
    coverage: str
    unimplemented: tuple[str, ...]
    sandbox: bool = False


PACKS = {
    'formation': Pack('formation_extended', 'formation_extended', 'host',
        'Real source formation, review, correction and lexical recollection; candidate extractor with fixed supporting review.',
        ('native_memory_writer', 'speech_recognition', 'semantic_retrieval')),
    'perspective': Pack('native_perspective_cases', 'native_perspective', 'perspective',
        'Durable appraisals with a fixed native reader; candidate reasoning applies updates to canonical sources.',
        ('automatic_opinion_projection', 'freeform_tone_grading', 'native_source_capture')),
    'planning': Pack('planning_effects', 'planning_effects', 'native',
        'Native tool planning checked against actual reservations in an owned offline inventory sandbox.',
        ('production_actions', 'long_horizon_planning'), sandbox=True),
    'recovery': Pack('recovery_cases', 'native_recovery', 'native',
        'Native tool recovery with durable inventory effects and bounded injected faults.',
        ('production_recovery', 'machine_reboot', 'channel_transport'), sandbox=True),
    'semantic': Pack('semantic_recall_cases', 'native_semantic_recall', 'semantic',
        'Real formation, semantic/lexical retrieval and native recollection; fixed supporting processors and retrieval endpoints.',
        ('native_memory_writer', 'live_channel_transport', 'natural_language_forget_dispatch')),
    'authority': Pack('native_authority_cases', 'native_authority', 'native',
        'Verified synthetic identities, real scoped host authorization and offline native tool effects.',
        ('physical_sender_verification', 'exhaustive_leak_prevention'), sandbox=True),
    'identity-audience': Pack('native_identity_cases', 'native_contact_identity', 'native',
        'Actual identity enrollment and audience-scoped recollection; supplied context and final disclosure scored separately.',
        ('channel_transport', 'exhaustive_leak_prevention')),
    'interactive': Pack('native_interactive_cases', 'native_interactive', 'native',
        'Real native gateway task transitions, foreground conversations and retained worker effects.',
        ('physical_channels', 'complete_global_task_inventory', 'delegated_child_agent_inheritance', 'capacity_load')),
    'unified': Pack('native_unified_cases', 'native_unified', 'native',
        'Four native shared-work scenarios and two base-Hermes comparison arms; six executions, four scenarios.',
        ('physical_channels', 'complete_global_task_inventory', 'delegated_child_agent_inheritance')),
    'router-recovery': Pack('router_recovery', 'router_recovery', 'router',
        'Existing router with declared faults and real candidate/fallback responses; fallback success is not a primary pass.',
        ('production_endpoint_failure', 'deployment_rebinding', 'machine_recovery')),
    'evidence': Pack('evidence_cases', 'evidence_cases', 'host',
        'Endpoint document grounding, planning and long-context evidence; no native tool effects.',
        ('native_tools', 'native_context_compression', 'model_maximum_context')),
    'interaction': Pack('interaction_evidence', 'interaction_evidence', 'host',
        'Endpoint visual evidence and fixed transcript reasoning; no actual speech transport.',
        ('speech_recognition', 'text_to_speech', 'live_voice_delivery')),
}


def modules(name):
    if name not in PACKS:
        raise ValueError('Select an installed qualification pack')
    spec = PACKS[name]
    return (spec, import_module('.' + spec.cases_module, __package__),
            import_module('.' + spec.consumer_module, __package__))
