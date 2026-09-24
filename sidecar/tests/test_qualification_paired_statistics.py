"""Scenario-level statistics reproduce exact values; nothing is simulated or fitted."""
import pytest

from protagine.qualification import paired_statistics as stats


@pytest.mark.parametrize('wins,losses,one_sided,two_sided', [
    (9, 1, 11 / 1024, 22 / 1024),      # ten disagreements, nine wins
    (6, 0, 1 / 64, 2 / 64),            # the smallest demonstrable outcome
    (5, 0, 1 / 32, 2 / 32),            # five clean wins are not significant two-sided
    (7, 1, 9 / 256, 18 / 256),
    (5, 5, 638 / 1024, 1.0),
    (1, 9, 1023 / 1024, 22 / 1024),    # losses reject just as strongly, but against the treatment
    (0, 0, 1.0, 1.0),
])
def test_exact_sign_test_reproduces_known_binomial_tails(wins, losses, one_sided, two_sided):
    result = stats.sign_test(wins, losses)
    assert result['p_one_sided'] == pytest.approx(one_sided)
    assert result['p_two_sided'] == pytest.approx(two_sided)
    assert result['non_tied_units'] == wins + losses


def test_significance_needs_six_wins_and_the_treatment_direction():
    assert stats.significant(6, 0)
    assert not stats.significant(5, 0)
    assert not stats.significant(1, 9)
    assert not stats.significant(9, 8)
    assert stats.significant(9, 1)


@pytest.mark.parametrize('n,expected', [
    (20, (0.15, 0.20, 0.20, 0.37)),
    (40, (0.35, 0.57, 0.84, 0.74)),
    (60, (0.52, 0.80, 0.99, 0.91)),
    (80, (0.68, 0.91, 1.00, 0.97)),
])
def test_power_reproduces_the_pre_registered_table(n, expected):
    columns = ((.35, .15), (.25, .05), (.20, .0), (.40, .10))
    observed = tuple(round(stats.power(n, win, loss), 2) for win, loss in columns)
    assert observed == expected


def test_power_is_exact_zero_when_the_arms_never_disagree_and_rises_with_units():
    assert stats.power(40, 0.0, 0.0) == 0
    assert stats.power(5, 1.0, 0.0) == 0   # under six units nothing can be demonstrated
    assert stats.power(6, 1.0, 0.0) == 1
    assert stats.power(40, .25, .05) < stats.power(80, .25, .05)
    with pytest.raises(ValueError):
        stats.power(40, .7, .5)


def test_minimum_detectable_effect_holds_the_observed_disagreement_rate():
    correlated = stats.minimum_detectable_effect(40, 0.2)
    independent = stats.minimum_detectable_effect(40, 0.5)
    assert correlated == 20 and independent == 32
    assert stats.power(40, *stats.rates_for_effect(.31, .5)) < 0.8 <= stats.power(40, *stats.rates_for_effect(.32, .5))
    assert stats.minimum_detectable_effect(80, 0.5) == 23
    assert stats.minimum_detectable_effect(40, 0.0) == 20
    assert stats.minimum_detectable_effect(5, 1.0) is None


def test_pilot_sizing_picks_the_smallest_powered_candidate_or_reports_underpowered():
    # Pilot disagreement 30% is the table's correlated column: 0.57 / 0.80 / 0.91.
    # The exact power at 60 is 0.7973, just under the target, so the rule picks 80.
    sized = stats.pilot_sizing(5, 1, 20)
    assert [round(sized['power'][n], 2) for n in (40, 60, 80)] == [0.57, 0.80, 0.91]
    assert sized['power'][60] < 0.8
    assert sized['n'] == 80 and sized['run_at'] == 80 and not sized['underpowered']
    assert stats.pilot_sizing(4, 0, 20)['n'] == 40
    # Independent outcomes (50% disagreement) never reach 80% at 80 units but stay above 50%.
    independent = stats.pilot_sizing(7, 3, 20)
    assert independent['n'] is None and independent['run_at'] == 40 and not independent['underpowered']
    assert [round(independent['power'][n], 2) for n in (40, 60, 80)] == [0.35, 0.52, 0.68]
    hopeless = stats.pilot_sizing(0, 0, 20, candidates=(6, 7))
    assert hopeless['n'] is None and hopeless['underpowered'] and hopeless['run_at'] == 6
    with pytest.raises(ValueError):
        stats.pilot_sizing(15, 10, 20)


def test_cluster_bootstrap_is_seeded_deterministic_and_resamples_whole_clusters():
    clusters = [[1.0, 1.0], [0.0, 0.0], [1.0, 0.0], [1.0], [0.0]]
    first = stats.cluster_bootstrap(clusters, seed=7, samples=500)
    second = stats.cluster_bootstrap(clusters, seed=7, samples=500)
    other = stats.cluster_bootstrap(clusters, seed=8, samples=500)
    assert first == second and first['clusters'] == 5
    assert (first['lower'], first['upper']) != (other['lower'], other['upper'])
    assert 0 <= first['lower'] <= first['upper'] <= 1
    constant = stats.cluster_bootstrap([[1.0], [1.0], [1.0]], seed=1, samples=100)
    assert constant['lower'] == constant['upper'] == 1.0
    with pytest.raises(ValueError):
        stats.cluster_bootstrap([], seed=1)


def units(wins, losses, ties):
    """Scenario values: wins are 1 vs 0, losses 0 vs 1, ties both 1."""
    values = {}
    for index in range(wins):
        values[f'w-{index:02d}'] = (1.0, 0.0)
    for index in range(losses):
        values[f'l-{index:02d}'] = (0.0, 1.0)
    for index in range(ties):
        values[f't-{index:02d}'] = (1.0, 1.0)
    return values


def test_contrast_demonstrated_needs_p_wins_and_interval_together():
    strong = stats.contrast(units(10, 0, 30), seed=3)
    assert strong['verdict'] == 'demonstrated'
    assert strong['delta_pp'] == 25 and strong['wins'] == 10 and strong['ties'] == 30
    assert strong['sign_test']['p_two_sided'] == pytest.approx(2 / 1024)
    assert strong['ci_pp']['lower'] > 0 and strong['mde_pp'] == 22
    assert strong['non_inferior_point_estimate']
    # Five clean wins fail the minimum-wins rule even though the interval excludes zero.
    five = stats.contrast(units(5, 0, 35), seed=3)
    assert five['verdict'] == 'not_demonstrated' and five['sign_test']['p_two_sided'] == pytest.approx(2 / 32)
    # Many wins with nearly as many losses are not significant.
    noisy = stats.contrast(units(12, 9, 19), seed=3)
    assert noisy['verdict'] == 'not_demonstrated' and noisy['wins'] == 12
    assert noisy['sign_test']['p_two_sided'] > 0.05
    losing = stats.contrast(units(2, 12, 26), seed=3)
    assert losing['verdict'] == 'not_demonstrated' and losing['delta_pp'] == -25
    assert not losing['non_inferior_point_estimate'] and losing['ci_pp']['upper'] < 0


def test_contrast_is_not_demonstrated_when_the_sign_test_favours_the_comparator():
    # Twenty-nine clean wins against fifty-one one-third losses: the mean delta and its
    # interval are positive, the two-sided test rejects, but the direction is the comparator's.
    values = {f'w-{index:02d}': (1.0, 0.0) for index in range(29)}
    values.update({f'l-{index:02d}': (0.0, 1 / 3) for index in range(51)})
    result = stats.contrast(values, seed=7)
    assert result['wins'] == 29 and result['losses'] == 51
    assert result['sign_test']['p_two_sided'] < 0.05 < result['sign_test']['p_one_sided']
    assert result['ci_pp']['lower'] > 0 and result['delta_pp'] == pytest.approx(15)
    assert result['verdict'] == 'not_demonstrated'
    assert not stats.significant(29, 51)


def test_contrast_averages_repetitions_before_counting_wins():
    # A scenario at 2/3 vs 1/3 is one win; 1/2 vs 1/2 is one tie.
    values = {'a': (2 / 3, 1 / 3), 'b': (0.5, 0.5), 'c': (1.0, 1.0)}
    result = stats.contrast(values, seed=1)
    assert result['wins'] == 1 and result['ties'] == 2 and result['losses'] == 0
    assert result['units'] == 3 and result['delta_pp'] == pytest.approx(100 / 9)
    assert result['verdict'] == 'not_demonstrated'
    with pytest.raises(ValueError):
        stats.contrast({}, seed=1)


def test_cluster_bootstrap_resamples_whole_campaigns():
    """Probes of one campaign share its training, so the interval resamples campaigns, not probes:
    with correlated probes the campaign-clustered interval is wider than per-probe resampling, while
    the sign test and the point estimate stay over probes."""
    units, clusters = {}, {}
    for campaign in range(8):
        for probe in range(8):
            key = f'c{campaign}:p{probe}'
            units[key] = (1.0 if campaign < 4 else 0.0, 0.0)
            clusters[key] = f'c{campaign}'
    by_probe = stats.contrast(units, seed=5)
    by_campaign = stats.contrast(units, seed=5, clusters=clusters)
    assert by_probe['clusters'] == by_probe['units'] == 64 and by_campaign['clusters'] == 8
    assert by_campaign['units'] == 64 and by_campaign['wins'] == by_probe['wins'] == 32
    assert by_campaign['sign_test'] == by_probe['sign_test'] and by_campaign['delta_pp'] == by_probe['delta_pp']
    width = lambda result: result['ci_pp']['upper'] - result['ci_pp']['lower']
    assert width(by_campaign) > 2 * width(by_probe)
    assert by_campaign['ci_pp']['clusters'] == 8
    with pytest.raises(ValueError, match='cluster'):
        stats.contrast(units, seed=5, clusters={key: 'c0' for key in list(units)[:-1]})
