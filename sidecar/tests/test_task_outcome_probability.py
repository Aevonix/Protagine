"""Fixed task events use prior evidence and retain failures in proper scores."""
import pytest

from protagine.self_model.expectations import ExpectationStore, ExpectationEngine
from test_forecast_learning import issue, observed


def forecast(store, key='one', **changes):
    return issue(store, key, domain='task_outcome', cohort=changes.pop('cohort', 'recipe-a'),
                 expectation='first attempt completes by fixed deadline', **changes)


def estimate(store, **changes):
    return store.estimate_task_probability(cohort='recipe-a', subject_person_id='owner',
        now=changes.pop('now', 1400), evidence_is_current=changes.pop('evidence_is_current', lambda *a:True),
        **changes)


def test_terminal_failure_scores_before_deadline_without_inventing_coverage(tmp_path):
    store = ExpectationStore(str(tmp_path/'p.db'))
    forecast(store)
    observed(store, observed_at=1050, recorded_at=1051, value=False)
    assert store.forecast_history('one')['forecasts'][0]['outcome'] == 'miss'
    assert estimate(store)['probability'] == pytest.approx(.56)
    assert ExpectationEngine(store).calibration_report()['domains']['task_outcome']['brier'] == .49
    issue(store, 'reply', domain='expected_reply')
    observed(store, 'reply', observed_at=1050, recorded_at=1051, value=False)
    assert store.forecast_history('reply')['forecasts'][0]['outcome'] == 'unresolved'


def test_only_prior_original_outcomes_train_and_revision_cannot_vote_twice(tmp_path):
    store = ExpectationStore(str(tmp_path/'p.db'))
    forecast(store)
    forecast(store, previous_revision=1, horizon=1150, issued_at=1020)
    observed(store, observed_at=1050, recorded_at=1051)
    assert estimate(store, now=1051)['sample_n'] == 0
    value = estimate(store)
    assert value['sample_n'] == 1 and value['probability'] == pytest.approx(.76)
    assert not value['calibrated']
    forecast(store, 'future', origin_at=1500, issued_at=1501, horizon=1600)
    observed(store, 'future', observed_at=1550, recorded_at=1551, value=False)
    assert estimate(store) == value


def test_late_completion_is_negative_for_fixed_event(tmp_path):
    store = ExpectationStore(str(tmp_path/'p.db'))
    forecast(store)
    observed(store, observed_at=1200, recorded_at=1201)
    assert estimate(store)['success_n'] == 0
    assert estimate(store)['sample_n'] == 1


def test_recipe_change_and_retracted_receipts_do_not_borrow_history(tmp_path):
    store = ExpectationStore(str(tmp_path/'p.db'))
    forecast(store, cohort='different-model-recipe')
    observed(store, observed_at=1050, recorded_at=1051)
    assert estimate(store)['sample_n'] == 0
    forecast(store, 'matching')
    observed(store, 'matching', observed_at=1050, recorded_at=1051)
    assert estimate(store)['sample_n'] == 1
    assert estimate(store, evidence_is_current=lambda *a:False)['sample_n'] == 0


def test_correction_is_append_only_and_unknown_does_not_mean_failure(tmp_path):
    store = ExpectationStore(str(tmp_path/'p.db'))
    original = forecast(store)
    observed(store, status='censored', value=None, reason='cancelled')
    assert estimate(store)['sample_n'] == 0
    observed(store, previous_revision=1, receipt_ref='receipt:correction',
             evidence_refs=['receipt:correction'], source_versions={'receipt:correction':'v2'},
             observed_at=1050, recorded_at=1250)
    assert estimate(store, now=1249)['sample_n'] == 0
    assert estimate(store)['probability'] == pytest.approx(.76)
    history = store.forecast_history('one')
    assert len(history['outcomes']) == 2
    assert history['forecasts'][0]['confidence'] == original.confidence


def test_outcome_without_known_provider_model_still_trains(tmp_path):
    store = ExpectationStore(str(tmp_path/'p.db'))
    forecast(store, model_provenance={'requested_role':'planning','served_model':None})
    observed(store, observed_at=1050, recorded_at=1051, value=False)
    value = estimate(store)
    assert (value['sample_n'], value['success_n']) == (1, 0)
    assert value['probability'] == pytest.approx(.56)
