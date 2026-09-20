"""Native recollection with the shipped semantic index and selector enabled."""
from contextlib import contextmanager
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from protagine.qualification.native_memory_worker import prepare as memory_prepare
from protagine.qualification.semantic_recall_host import setup
from protagine.qualification.native_worker import main


@contextmanager
def prepare(request, state, arguments, config):
    retrieval = {}

    def setup_host(app, owned_state, inputs, configuration):
        return setup(app, owned_state, inputs, configuration, retrieval)

    with memory_prepare(request, state, arguments, config, setup_host=setup_host) as observe:
        def evidence(agent, response):
            return {**observe(agent, response), 'semantic_recall': retrieval,
                    'embedding_and_reranking': 'real_pipeline_observed'}
        yield evidence


if __name__ == '__main__':
    raise SystemExit(main(prepare))
