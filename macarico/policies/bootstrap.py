from __future__ import division, generators, print_function

from macarico.policies.linear import LinearPolicy
from macarico import Policy
from macarico import util

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.autograd import Variable as Var


def actions_to_probs(actions, n_actions):
    probs = torch.zeros(n_actions)
    bag_size = len(actions)
    prob = 1. / bag_size
    for action_set in actions:
        for action in action_set:
            probs[action] += prob / len(action_set)
    return probs


# Randomize over predictions from a base set of predictors
def bootstrap_probabilities(n_actions, policy_bag, state, deviate_to):
    actions = [[policy(state, deviate_to)] for policy in policy_bag]
    probs = actions_to_probs(actions, n_actions)
    return probs


def min_set(costs, limit_actions=None):
    min_val = None
    min_set = []
    if limit_actions is None:
        for a, c in enumerate(costs):
            if min_val is None or c < min_val:
                min_val = c
                min_set = [a]
            elif c == min_val:
                min_set.append(a)
    else:
        for a in limit_actions:
            c = costs[a]
            if min_val is None or c < min_val:
                min_val = c
                min_set = [a]
            elif c == min_val:
                min_set.append(a)
    return min_set

class BootstrapCost:
    def __init__(self, costs, greedy_predict=True):
        self.costs = costs
        self.greedy_predict = greedy_predict

    def average_cost(self):
        return sum(self.costs) / len(self.costs)
        
    def data(self):
        if self.greedy_predict:
            return self.costs[0].data
        else:
            return self.average_cost().data

    def get_probs(self, limit_actions=None):
        assert(len(self.costs) > 0)
        n_actions = len(self.costs[0].data)
        actions = [min_set(c.data, limit_actions) for c in self.costs]
        return actions_to_probs(actions, n_actions)

    def __getitem__(self, idx):
        if self.greedy_predict:
            return self.costs[0][idx]
        else:
            return self.average_cost()[idx]

    def __neg__(self):
        if self.greedy_predict:
            return self.costs[0].__neg__()
        else:
            return self.average_cost().__neg__()

    def argmin(self):
        if self.greedy_predict:
            return self.costs[0].argmin()
        else:
            return self.average_cost().argmin()

# Constructs a policy bag of linear policies, number of policies =
# len(features_bag)
def build_policy_bag(features_bag, n_actions, loss_fn, n_layers,
                     hidden_dim):
    return [LinearPolicy(features, n_actions, loss_fn=loss_fn,
                         n_layers=n_layers, hidden_dim=hidden_dim)
            for features in features_bag]


def delegate_with_poisson(params, functions, greedy_update):
    total_loss = 0.0
    functions_params_pairs = zip(functions, params)
    for idx, (loss_fn, params) in enumerate(functions_params_pairs):
        loss_i = loss_fn(*params)
        if greedy_update and idx == 0:
            count_i = 1
        else:
            count_i = np.random.poisson(1)
        total_loss = total_loss + count_i * loss_i
    return total_loss


class BootstrapPolicy(nn.Module, Policy):
    """
        Bootstrapping policy
    """

    def __init__(self, features_bag, n_actions, loss_fn='squared',
                 greedy_predict=True, greedy_update=True, n_layers=1,
                 hidden_dim=50):
        nn.Module.__init__(self)
        
        self.n_actions = n_actions
        self.bag_size = len(features_bag)
        self.policy_bag = nn.ModuleList(build_policy_bag(features_bag, n_actions,
                                           loss_fn, n_layers, hidden_dim))
        self.greedy_predict = greedy_predict
        self.greedy_update = greedy_update

    def __call__(self, state, deviate_to=None):
        action_probs = bootstrap_probabilities(self.n_actions, self.policy_bag,
                                               state, deviate_to)
        action = None
        if self.greedy_predict:
            action = self.policy_bag[0](state, deviate_to)
        else:
            action, prob = util.sample_from_np_probs(action_probs)
        return action

    def predict_costs(self, state, deviate_to=None):
        all_costs = [policy.predict_costs(state, deviate_to)
                     for policy in self.policy_bag]
        return BootstrapCost(all_costs, self.greedy_predict)

    def greedy(self, state, pred_costs=None, deviate_to=None):
        actions = [[policy.greedy(state, pred_costs=p_costs,
                                  deviate_to=deviate_to)]
                   for policy, p_costs in zip(self.policy_bag, pred_costs)]
        action_probs = actions_to_probs(actions, self.n_actions)
        action = None
        if self.greedy_predict:
            action = self.policy_bag[0].greedy(state, pred_costs=pred_costs[0],
                                               deviate_to=deviate_to)
        else:
            action, prob = util.sample_from_np_probs(action_probs)
        return action

    def forward(self, state, ref):
        params = [(state, ref) for i in range(self.bag_size)]
        fns = [policy.forward for policy in self.policy_bag]
        return delegate_with_poisson(params, fns, self.greedy_update)

    def forward_partial_complete(self, costs, truth, acts):
        params = [(costs.costs[i], truth, acts) for i in range(self.bag_size)]
        loss_fns = [p.forward_partial_complete for p in self.policy_bag]
        return delegate_with_poisson(params, loss_fns, self.greedy_update)
