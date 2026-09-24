"""One statistics method for every paired contrast: unit-level sign test,
cluster bootstrap interval, power and minimum detectable effect.

Units are scenarios, or the probes of campaigns clustered by campaign.
Repetitions of one unit are averaged into one value before anything is tested. Every function is exact or
seeded, so a report is reproducible from its results directory alone.
"""
import math
import random

ALPHA = 0.05
MIN_WINS = 6
CI_LEVEL = 0.95
BOOTSTRAP_SAMPLES = 2000
POWER_TARGET = 0.8
EFFECT_PP = 20
CANDIDATE_N = (40, 60, 80)


def binomial_tail(successes, trials):
    """P[X >= successes] for X ~ Binomial(trials, 1/2)."""
    if trials == 0:
        return 1.0
    return sum(math.comb(trials, k) for k in range(successes, trials + 1)) / 2 ** trials


def sign_test(wins, losses):
    """Exact sign test over non-tied units; ties carry no information."""
    if type(wins) is not int or type(losses) is not int or wins < 0 or losses < 0:
        raise ValueError('Wins and losses are non-negative counts')
    trials = wins + losses
    one_sided = binomial_tail(wins, trials)
    two_sided = min(1.0, 2 * binomial_tail(max(wins, losses), trials))
    return {'non_tied_units': trials, 'wins': wins, 'losses': losses,
            'p_one_sided': one_sided, 'p_two_sided': two_sided}


def significant(wins, losses, *, alpha=ALPHA, min_wins=MIN_WINS):
    """Rejection in the treatment's favour under the pre-registered rule."""
    return wins >= min_wins and wins > losses and sign_test(wins, losses)['p_two_sided'] < alpha


def power(n, win, loss, *, alpha=ALPHA, min_wins=MIN_WINS):
    """Exact power of the pre-registered sign test at n units.

    win and loss are the per-unit probabilities that the treatment beats or
    trails the comparator; the rest are ties. Enumerates every (wins, losses)
    outcome of the trinomial, so no simulation noise enters a sizing decision.
    """
    if type(n) is not int or n < 1:
        raise ValueError('Units must be a positive count')
    if not (0 <= win <= 1 and 0 <= loss <= 1 and win + loss <= 1 + 1e-12):
        raise ValueError('Win and loss probabilities must be within [0, 1] and sum to at most 1')
    tie = max(0.0, 1 - win - loss)
    # Fewest wins that reject at each number of non-tied units.
    critical = {}
    for trials in range(n + 1):
        critical[trials] = next((wins for wins in range(trials + 1)
                                 if significant(wins, trials - wins, alpha=alpha, min_wins=min_wins)), None)
    total = 0.0
    for wins in range(min_wins, n + 1):
        for losses in range(0, n - wins + 1):
            needed = critical[wins + losses]
            if needed is None or wins < needed:
                continue
            ties = n - wins - losses
            total += (math.comb(n, wins) * math.comb(n - wins, losses)
                      * win ** wins * loss ** losses * tie ** ties)
    return min(1.0, total)


def rates_for_effect(effect, disagreement):
    """Per-unit win and loss rates for an effect at a given disagreement rate.

    effect and disagreement are fractions. When the arms disagree on fewer
    units than the effect needs, every disagreement must be a win.
    """
    if effect > disagreement:
        return effect, 0.0
    return (disagreement + effect) / 2, (disagreement - effect) / 2


def minimum_detectable_effect(n, disagreement, *, target=POWER_TARGET, alpha=ALPHA, min_wins=MIN_WINS):
    """Smallest treatment-minus-comparator difference (pp) found with the target power.

    Holds the observed disagreement rate (wins + losses over units), which is
    what makes a paired sign test powerful or not. Returns None when even a
    100 pp effect stays under the target power at this n.

    Models one binary outcome per unit, so a non-tied unit moves the pass rate
    by a whole unit. Repetition means move it by a fraction, and the sign test
    sees only the direction, so for repeated scenarios this is an upper bound
    on the pass-rate difference the test finds with the target power.
    """
    def enough(effect_pp):
        win, loss = rates_for_effect(effect_pp / 100, disagreement)
        return power(n, win, loss, alpha=alpha, min_wins=min_wins) >= target

    if not enough(100):
        return None
    low, high = 1, 100  # power rises with the effect, so bisect the smallest sufficient one
    while low < high:
        middle = (low + high) // 2
        if enough(middle):
            high = middle
        else:
            low = middle + 1
    return low


def pilot_sizing(pilot_wins, pilot_losses, pilot_units, *, effect_pp=EFFECT_PP,
                 candidates=CANDIDATE_N, target=POWER_TARGET):
    """Pick the smallest n in the candidates with the target power for the effect.

    Uses the pilot's disagreement rate. A family with no candidate over 50%
    power still runs at the smallest candidate and reports as underpowered.
    """
    if type(pilot_units) is not int or pilot_units < 1 or pilot_wins + pilot_losses > pilot_units:
        raise ValueError('Pilot counts must fit its units')
    disagreement = (pilot_wins + pilot_losses) / pilot_units
    win, loss = rates_for_effect(effect_pp / 100, disagreement)
    powers = {n: power(n, win, loss) for n in candidates}
    chosen = next((n for n in candidates if powers[n] >= target), None)
    return {'effect_pp': effect_pp, 'pilot_disagreement': disagreement, 'power': powers,
            'n': chosen, 'underpowered': max(powers.values()) < 0.5,
            'run_at': chosen if chosen is not None else min(candidates)}


def cluster_bootstrap(clusters, *, seed, samples=BOOTSTRAP_SAMPLES, level=CI_LEVEL):
    """Percentile interval of the mean over units, resampling whole clusters.

    Clusters are scenarios, or campaigns whose probes are correlated. Seeded
    with the plan identity, so two reports of one directory agree exactly.
    """
    clusters = [list(cluster) for cluster in clusters if cluster]
    if not clusters or type(samples) is not int or samples < 100:
        raise ValueError('Bootstrap needs nonempty clusters and at least 100 samples')
    generator = random.Random(seed)
    means = []
    count = len(clusters)
    for _ in range(samples):
        chosen = [clusters[generator.randrange(count)] for _ in range(count)]
        values = [value for cluster in chosen for value in cluster]
        means.append(sum(values) / len(values))
    means.sort()
    lower = means[int((1 - level) / 2 * (samples - 1))]
    upper = means[int((1 + level) / 2 * (samples - 1))]
    return {'lower': lower, 'upper': upper, 'level': level, 'samples': samples,
            'clusters': count, 'seed': seed, 'method': 'percentile_cluster_bootstrap'}


def contrast(units, *, seed, clusters=None, alpha=ALPHA, min_wins=MIN_WINS, non_inferior_pp=-10):
    """Test one treatment against one comparator over unit-level pass values.

    units: {unit: (treatment_pass, comparator_pass)} with each value the mean
    pass over that unit's repetitions (a scenario, or a campaign's probe).
    clusters: {unit: cluster} when units are correlated within a cluster (the
    probes of one campaign); the interval then resamples whole clusters of
    unit deltas. The sign test, the point estimate and the MDE stay over units
    (the MDE ignores clustering). Returns the pre-registered verdict with its
    evidence; a demonstrated verdict needs all three rules.
    """
    if not units:
        raise ValueError('A contrast needs at least one unit')
    deltas = {key: treatment - comparator for key, (treatment, comparator) in units.items()}
    if clusters is None:
        groups = [[delta] for delta in deltas.values()]
    else:
        if not isinstance(clusters, dict) or set(clusters) != set(deltas):
            raise ValueError('Every unit belongs to exactly one declared cluster')
        grouped = {}
        for key, delta in deltas.items():
            grouped.setdefault(clusters[key], []).append(delta)
        groups = list(grouped.values())
    wins = sum(delta > 0 for delta in deltas.values())
    losses = sum(delta < 0 for delta in deltas.values())
    ties = len(deltas) - wins - losses
    count = len(deltas)
    treatment_rate = sum(value[0] for value in units.values()) / count
    comparator_rate = sum(value[1] for value in units.values()) / count
    delta_pp = 100 * (treatment_rate - comparator_rate)
    test = sign_test(wins, losses)
    interval = cluster_bootstrap(groups, seed=seed)
    interval_pp = {**interval, 'lower': 100 * interval['lower'], 'upper': 100 * interval['upper']}
    disagreement = (wins + losses) / count
    # The pre-registered rejection is in the treatment's favour: a two-sided
    # rejection with more losses than wins is evidence for the comparator.
    demonstrated = significant(wins, losses, alpha=alpha, min_wins=min_wins) and interval_pp['lower'] > 0
    return {'units': count, 'clusters': len(groups), 'wins': wins, 'losses': losses, 'ties': ties,
            'treatment_pass_rate': treatment_rate, 'comparator_pass_rate': comparator_rate,
            'delta_pp': delta_pp, 'sign_test': test, 'ci_pp': interval_pp,
            'disagreement_rate': disagreement,
            'mde_pp': minimum_detectable_effect(count, disagreement, alpha=alpha, min_wins=min_wins),
            'non_inferior_point_estimate': delta_pp >= non_inferior_pp,
            'verdict': 'demonstrated' if demonstrated else 'not_demonstrated'}
