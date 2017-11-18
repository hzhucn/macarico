from __future__ import division, generators, print_function

import macarico

class Example(object):
    """
    >>> e = Example('abcdef', 'ABCDEF', 7)
    >>> env = e.mk_env()
    >>> env.run_episode(env.reference())
    ['A', 'B', 'C', 'D', 'E', 'F']
    >>> env.loss()
    0.0
    >>> env = e.mk_env()
    >>> env.run_episode(lambda s: s.tokens[s.n].upper() if s.n % 2 else '_')
    ['_', 'B', '_', 'D', '_', 'F']
    >>> env.loss()
    0.5
    """

    def __init__(self, tokens, labels, n_labels, label_dict=None, token_dict=None):
        self.tokens = tokens
        self.labels = labels
        self.n_labels = n_labels
        self.label_dict = label_dict
        self.token_dict = token_dict

    def mk_env(self):
        return SequenceLabeling(self, self.n_labels)

    def __str__(self):
        if self.label_dict is not None:
            return ' '.join(map(self.label_dict, self.labels[:-1]))
        return ' '.join(map(str, self.labels[:-1]))

    def input_x(self):
        if self.token_dict is not None:
            return ' '.join(map(self.token_dict, self.tokens[:-1]))
        return ' '.join(map(str, self.tokens[:-1]))


class SequenceLabeling(macarico.Env):
    """Basic sequence labeling environment (input and output sequences have the same
    length). Loss is evaluated with Hamming distance, which has an optimal
    reference policy.

    """

    def __init__(self, example, n_labels):
        macarico.Env.__init__(self, n_labels)
        self.example = example
        self.N = len(example.tokens)
        self.n = None
        self.tokens = example.tokens
        self.actions = set(range(n_labels))

    def horizon(self):
        return self.N
        
    def _rewind(self):
        self.n = None
        self._trajectory = []

    def _run_episode(self, policy):
        self._trajectory = []
        for self.n in range(self.N):
            a = policy(self)
            self._trajectory.append(a)
        return self._trajectory

    def input_x(self):
        return self.example.input_x()
    


class HammingLossReference(macarico.Reference):
    def __call__(self, state):
        return int(state.example.labels[state.n])

    def set_min_costs_to_go(self, state, cost_vector):
        cost_vector *= 0
        cost_vector += 1
        cost_vector[state.example.labels[state.n]] = 0.

class HammingLoss(macarico.Loss):
    def __init__(self):
        super(HammingLoss, self).__init__('hamming')
    
    def evaluate(self, ex, state):
        assert len(state._trajectory) == len(ex.labels), 'can only evaluate loss at final state'
        return sum(y != p for p,y in zip(state._trajectory, ex.labels))

