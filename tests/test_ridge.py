import json

import numpy as np

from power_forecast import ridge
from power_forecast.models import FeatureCache, build


def test_numpy_ridge_matches_the_research_pipeline(archive):
    cache = FeatureCache(archive)
    days = sorted(archive)
    x, y = cache.matrix(days[0], days[40])
    test_x, _ = cache.matrix(days[41], days[-1])
    reference = build("ridge", {"alpha": 1.0}, 0).fit(x, y).predict(test_x)
    params = json.loads(json.dumps(ridge.fit(x, y, 1.0)))  # survives a JSON round trip
    np.testing.assert_allclose(ridge.predict(params, test_x), reference, rtol=1e-7, atol=1e-7)


def test_unseen_category_encodes_as_zeros(archive):
    cache = FeatureCache(archive)
    days = sorted(archive)
    x, y = cache.matrix(days[0], days[20])
    params = ridge.fit(x, y, 1.0)
    row = x[:1].copy()
    row[0, 1] = 99  # a weekday never seen in training
    reference = build("ridge", {"alpha": 1.0}, 0).fit(x, y).predict(row)
    np.testing.assert_allclose(ridge.predict(params, row), reference, rtol=1e-7, atol=1e-7)
