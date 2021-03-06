"""Classification and regression trees"""

# Author: Mathieu Blondel <mathieu@mblondel.org>
# License: BSD 3 clause

import numbers

import numpy as np
import numba

from sklearn.base import BaseEstimator
from sklearn.base import RegressorMixin
from sklearn.base import ClassifierMixin
from sklearn.preprocessing import LabelEncoder
from sklearn.utils import check_random_state

from .sort import heapsort

TREE_LEAF = -1
UNDEFINED = -2

DOUBLE_MAX = np.finfo(np.float64).max

MSE_CRITERION = 0
GINI_CRITERION = 1
ENTROPY_CRITERION = 2


class _Tree(object):

    def __init__(self, capacity=2 ** 10):
        self.capacity = capacity
        self.threshold = np.zeros(capacity, dtype=np.float64) + UNDEFINED
        self.feature = np.zeros(capacity, dtype=np.int32) + UNDEFINED
        self.children_left = np.zeros(capacity, dtype=np.int32) + TREE_LEAF
        self.children_right = np.zeros(capacity, dtype=np.int32) + TREE_LEAF
        self.value = np.zeros(capacity, dtype=np.float64)
        self.ptr = 0

    def add_node(self, threshold, feature, value):
        self.threshold[self.ptr] = threshold
        self.feature[self.ptr] = feature
        self.value[self.ptr] = value
        self.ptr += 1

    def add_terminal_node(self, value):
        self.value[self.ptr] = value
        self.ptr += 1

    def finalize(self):
        for attr in ("threshold", "feature", "value",
                     "children_left", "children_right"):
            attr_value = getattr(self, attr)[:self.ptr + 1]
            setattr(self, attr, attr_value)
        return self


class _Stack(object):

    def __init__(self, capacity=2 ** 10):
        self.capacity = capacity
        self.start = np.zeros(capacity, dtype=np.int32)
        self.end = np.zeros(capacity, dtype=np.int32)
        self.left = np.zeros(capacity, dtype=bool)
        self.depth = np.zeros(capacity, dtype=np.int32)
        self.n_samples = np.zeros(capacity, dtype=np.float64)
        self.parent = np.zeros(capacity, dtype=np.int32)
        self.value = np.zeros(capacity, dtype=np.float64)
        self.ptr = -1

    def push(self, start, end, left, depth, n_samples, parent, value):
        if self.ptr >= self.capacity:
            raise ValueError("Stack overflow!")

        self.ptr += 1
        self.start[self.ptr] = start
        self.end[self.ptr] = end
        self.left[self.ptr] = left
        self.depth[self.ptr] = depth
        self.n_samples[self.ptr] = n_samples
        self.parent[self.ptr] = parent
        self.value[self.ptr] = value

    def pop(self):
        self.ptr -= 1
        p = self.ptr + 1
        return self.start[p], self.end[p], self.left[p], self.depth[p], \
               self.n_samples[p], self.parent[p], self.value[p]

    def __len__(self):
        return self.ptr + 1


@numba.njit("void(f8[:,:], i4[:], f8[:], i4[:], i4[:], i4[:])")
def _apply(X, feature, threshold, children_left, children_right, out):
    for i in range(X.shape[0]):
        node = 0
        # While node not a leaf
        while children_left[node] != TREE_LEAF:
            if X[i, feature[node]] <= threshold[node]:
                node = children_left[node]
            else:
                node = children_right[node]
        out[i] = node


@numba.njit("f8(f8[:], f8[:], f8[:], i4[:], i4, i4, i4, f8[:])")
def _impurity_mse(Xj, y, sample_weight, samples, start_t, pos_t, end_t, out):
    N_L = 0
    N_R = 0

    y_sq = 0
    y_sum = 0

    for ii in xrange(start_t, pos_t):
        i = samples[ii]
        N_L += sample_weight[i]
        y_sq += sample_weight[i] * y[i] * y[i]
        y_sum += sample_weight[i] * y[i]

    if N_L == 0:
        return DOUBLE_MAX

    value_L = y_sum / N_L
    imp_L = y_sq - 1 * y_sum * y_sum / N_L

    y_sq = 0
    y_sum = 0

    for ii in xrange(pos_t, end_t):
        i = samples[ii]
        N_R += sample_weight[i]
        y_sq += sample_weight[i] * y[i] * y[i]
        y_sum += sample_weight[i] * y[i]

    if N_R == 0:
        return DOUBLE_MAX

    value_R = y_sum / N_R
    imp_R = y_sq - 1 * y_sum * y_sum / N_R

    N_t = N_L + N_R

    out[0] = N_L
    out[1] = N_R
    out[2] = N_t
    out[3] = value_L
    out[4] = value_R

    return (imp_L + imp_R) / N_t


@numba.njit("void(f8[:], f8[:], f8[:], i4[:], i4, i4, f8, f8[:], f8[:], f8[:])")
def _compute_counts(Xj, y, sample_weight, samples, start_t, pos_t, end_t,
                    count_L, count_R, out):
    n_classes = count_L.shape[0]
    N_L = 0
    N_R = 0

    for k in xrange(n_classes):
        count_L[k] = 0
        count_R[k] = 0

    for ii in xrange(start_t, pos_t):
        i = samples[ii]
        N_L += sample_weight[i]
        idx = int(y[i])
        count_L[idx] += sample_weight[i]

    for ii in xrange(pos_t, end_t):
        i = samples[ii]
        N_R += sample_weight[i]
        idx = int(y[i])
        count_R[idx] += sample_weight[i]

    best_L = -DOUBLE_MAX
    best_R = -DOUBLE_MAX
    value_L = 0
    value_R = 0

    for k in xrange(n_classes):
        if count_L[k] > best_L:
            best_L = count_L[k]
            value_L = k

        if count_R[k] > best_R:
            best_R = count_R[k]
            value_R = k

    out[0] = N_L
    out[1] = N_R
    out[2] = N_L + N_R
    out[3] = value_L
    out[4] = value_R


@numba.njit("f8(f8[:], f8[:], f8[:], i4[:], i4, i4, i4, f8[:], f8[:], f8[:])")
def _impurity_gini(Xj, y, sample_weight, samples, start_t, pos_t, end_t,
                   count_L, count_R, out):
    n_classes = count_L.shape[0]

    _compute_counts(Xj, y, sample_weight, samples, start_t, pos_t, end_t,
                    count_L, count_R, out)
    N_L = out[0]
    N_R = out[1]
    N_t = out[2]

    if N_L == 0 and N_R == 0:
        return DOUBLE_MAX

    gini_L = 0
    gini_R = 0
    for k in xrange(n_classes):
        proba_L = count_L[k] / N_t
        proba_R = count_R[k] / N_t

        gini_L += proba_L * (1 - proba_L)
        gini_R += proba_R * (1 - proba_R)

    #return float(N_L) / N_t * gini_L + float(N_R) / N_t * gini_R
    return N_L * gini_L + N_R * gini_R


@numba.njit("f8(f8[:], f8[:], f8[:], i4[:], i4, i4, i4, f8[:], f8[:], f8[:])")
def _impurity_entropy(Xj, y, sample_weight, samples, start_t, pos_t, end_t,
                      count_L, count_R, out):
    n_classes = count_L.shape[0]

    _compute_counts(Xj, y, sample_weight, samples, start_t, pos_t, end_t,
                    count_L, count_R, out)
    N_L = out[0]
    N_R = out[1]
    N_t = out[2]

    if N_L == 0 or N_R == 0:
        return DOUBLE_MAX

    ent_L = 0
    ent_R = 0
    for k in xrange(n_classes):
        proba_L = count_L[k] / N_t
        proba_R = count_R[k] / N_t

        if proba_L > 0:
            ent_L -= proba_L * np.log2(proba_L)

        if proba_R > 0:
            ent_R -= proba_R * np.log2(proba_R)

    #return float(N_L) / N_t * ent_L + float(N_R) / N_t * ent_R
    return N_L * ent_L + N_R * ent_R


@numba.njit("void(f8[:,:], f8[:], f8[:], i4[:], i4[:], f8[:], i4, i4, i4, "
            "i4, f8[:], f8[:], f8[:])")
def _best_split(X, y, sample_weight, samples, features, Xj, start_t, end_t,
                criterion, min_samples_leaf, count_L, count_R, out):
    best_imp = DOUBLE_MAX
    best_thresh = 0.0
    best_j = -1
    best_pos_t = -1
    N_L = 0.0
    N_R = 0.0
    N_t = 0.0
    value_L = 0.0
    value_R = 0.0

    size_t = end_t - start_t

    for j in features:

        for p in xrange(start_t, end_t):
            Xj[p] = X[samples[p], j]

        # Sort samples in nodes_t by their value for feature j.
        heapsort(Xj[start_t:end_t], samples[start_t:end_t], size_t)
        # FIXME: use introsort.

        for k in xrange(start_t, end_t - 1):
            pos_t = k + 1
            N_L = pos_t - start_t
            N_R = size_t - N_L

            if N_R < min_samples_leaf or N_L < min_samples_leaf:
                continue

            # Choose splitting threshold.
            # Any value between Xj[k+1] and Xj[k] is fine.
            Xj_diff = Xj[k + 1] - Xj[k]

            if Xj_diff == 0:
                continue

            thresh = Xj_diff / 2.0 + Xj[k]

            # FIXME: impurity can be computed efficiently from last
            # iteration.
            if criterion == MSE_CRITERION:
                imp = _impurity_mse(Xj, y, sample_weight, samples, start_t,
                                    pos_t, end_t, out)
            elif criterion == GINI_CRITERION:
                imp = _impurity_gini(Xj, y, sample_weight, samples, start_t,
                                     pos_t, end_t, count_L, count_R, out)
            else:
                imp = _impurity_entropy(Xj, y, sample_weight, samples, start_t,
                                        pos_t, end_t, count_L, count_R, out)

            if imp < best_imp:
                best_imp = imp
                best_thresh = thresh
                best_j = j
                best_pos_t = pos_t
                N_L = out[0]
                N_R = out[1]
                N_t = out[2]
                value_L = out[3]
                value_R = out[4]

    out[0] = N_L
    out[1] = N_R
    out[2] = N_t
    out[3] = value_L
    out[4] = value_R
    out[5] = best_thresh
    out[6] = best_j
    out[7] = best_pos_t

    if best_j != -1:
        # Reorder samples for the best split.
        for p in xrange(start_t, end_t):
            Xj[p] = X[samples[p], best_j]

        heapsort(Xj[start_t:end_t], samples[start_t:end_t], size_t)


def _build_tree(X, y, sample_weight, criterion, max_features=None,
                max_depth=None, min_samples_split=2, min_samples_leaf=1,
                random_state=None):
    n_samples, n_features = X.shape

    tree = _Tree()
    node_t = 0
    samples = np.arange(n_samples).astype(np.int32)
    samples = samples[sample_weight > 0]
    features = np.arange(n_features).astype(np.int32)

    stack = _Stack()
    stack.push(start=0, end=len(samples), left=False,
               depth=0, n_samples=np.sum(sample_weight),
               parent=0, value=0)

    # Buffers
    Xj = np.zeros(n_samples, dtype=np.float64)
    out = np.zeros(8, dtype=np.float64)

    if criterion >= GINI_CRITERION:  # Classification case
        enc = LabelEncoder()
        y = enc.fit_transform(y).astype(np.float64)
        # Arrays which will contain the number of samples in each class.
        count_L = np.zeros(len(enc.classes_), dtype=np.float64)
        count_R = np.zeros(len(enc.classes_), dtype=np.float64)
    else:
        count_L = np.zeros(0, dtype=np.float64)
        count_R = np.zeros(0, dtype=np.float64)

    while len(stack) > 0:
        # Pick node from the stack.
        start_t, end_t, left_t, depth_t, N_t, parent_t, value_t = stack.pop()

        if node_t > 0:
            # Adjust children node id of parent.
            if left_t:
                tree.children_left[parent_t] = node_t
            else:
                tree.children_right[parent_t] = node_t

        size_t = end_t - start_t

        # Terminal node if max_depth or min_samples_split conditions are met.
        if depth_t == max_depth or size_t < min_samples_split:
            tree.add_terminal_node(value_t)
            node_t += 1
            continue

        # Find best split across all features.
        if max_features != n_features:
            random_state.shuffle(features)

        _best_split(X, y, sample_weight, samples, features[:max_features], Xj,
                    start_t, end_t, criterion, min_samples_leaf,
                    count_L, count_R, out)
        N_L, N_R, _, value_L, value_R, best_thresh, best_j, pos_t = out
        best_j = int(best_j)
        pos_t = int(pos_t)

        # No best split found: terminal node.
        if best_j == -1:
            tree.add_terminal_node(value_t)
            node_t += 1
            continue

        # Add node to the tree.
        tree.add_node(threshold=best_thresh, feature=best_j, value=value_t)

        # Add left and right children to the stack.
        stack.push(start=start_t, end=pos_t, left=True, depth=depth_t + 1,
                   n_samples=N_L, parent=node_t, value=value_L)
        stack.push(start=pos_t, end=end_t, left=False, depth=depth_t + 1,
                   n_samples=N_R, parent=node_t, value=value_R)

        node_t += 1

    if criterion >= GINI_CRITERION:
        values = np.array(tree.value, dtype=np.int32)
        tree.value = enc.inverse_transform(values)

    return tree.finalize()


class _BaseTree(BaseEstimator):

    def _get_max_features(self, X):
        n_features = X.shape[1]

        if self.max_features is None:
            max_features = n_features
        elif isinstance(self.max_features, (numbers.Integral, np.integer)):
            max_features = self.max_features
        else:  # float
            if self.max_features > 0.0:
                max_features = max(1, int(self.max_features * n_features))
            else:
                raise ValueError("max_features should be positive!")

        return max_features


class TreeClassifier(_BaseTree, ClassifierMixin):

    def __init__(self, criterion="gini", max_features=None, max_depth=None,
                 min_samples_split=2, min_samples_leaf=1, random_state=None):
        self.criterion = criterion
        self.max_features = max_features
        self.max_depth = max_depth
        self.min_samples_split = min_samples_split
        self.min_samples_leaf = min_samples_leaf
        self.random_state = random_state

    def _get_criterion(self):
        return {"gini": GINI_CRITERION,
                "entropy": ENTROPY_CRITERION}[self.criterion]

    def fit(self, X, y, sample_weight=None):
        rng = check_random_state(self.random_state)

        if sample_weight is None:
            sample_weight = np.ones(X.shape[0], dtype=np.float64)

        self.tree_ = _build_tree(X, y, sample_weight,
                                 criterion=self._get_criterion(),
                                 max_features=self._get_max_features(X),
                                 max_depth=self.max_depth,
                                 min_samples_split=self.min_samples_split,
                                 min_samples_leaf=self.min_samples_leaf,
                                 random_state=rng)
        self.tree_.value = self.tree_.value.astype(np.int32)
        return self

    def predict(self, X):
        nodes = np.empty(X.shape[0], dtype=np.int32)
        _apply(X, self.tree_.feature, self.tree_.threshold,
               self.tree_.children_left, self.tree_.children_right, nodes)
        return self.tree_.value.take(nodes)


class TreeRegressor(_BaseTree, RegressorMixin):

    def __init__(self, max_features=None, max_depth=None, min_samples_split=2,
                 min_samples_leaf=1, random_state=None):
        self.max_features = max_features
        self.max_depth = max_depth
        self.min_samples_split = min_samples_split
        self.min_samples_leaf = min_samples_leaf
        self.random_state = random_state

    def fit(self, X, y, sample_weight=None):
        rng = check_random_state(self.random_state)

        if sample_weight is None:
            sample_weight = np.ones(X.shape[0], dtype=np.float64)

        self.tree_ = _build_tree(X, y, sample_weight,
                                 criterion=MSE_CRITERION,
                                 max_features=self._get_max_features(X),
                                 max_depth=self.max_depth,
                                 min_samples_split=self.min_samples_split,
                                 min_samples_leaf=self.min_samples_leaf,
                                 random_state=rng)
        return self

    def predict(self, X):
        nodes = np.empty(X.shape[0], dtype=np.int32)
        _apply(X, self.tree_.feature, self.tree_.threshold,
               self.tree_.children_left, self.tree_.children_right, nodes)
        return self.tree_.value.take(nodes)
