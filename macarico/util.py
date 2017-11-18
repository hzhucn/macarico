from __future__ import division, generators, print_function
import random
import sys
import itertools
from copy import deepcopy
import macarico
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import progressbar
import time

from macarico.lts.lols import EpisodeRunner, one_step_deviation
from macarico.annealing import Averaging


# helpful functions
Var = torch.autograd.Variable

def Varng(*args, **kwargs):
    return torch.autograd.Variable(*args, **kwargs, requires_grad=False)

def getnew(param):
    return param.new if hasattr(param, 'new') else \
        param.data.new if hasattr(param, 'data') else \
        param.weight.data.new if hasattr(param, 'weight') else \
        None

def zeros(param, *dims):
    return getnew(param)(*dims).zero_()

def longtensor(param, lst):
    return getnew(param)(lst).long()

def onehot(param, i):
    return Varng(longtensor(param, [int(i)]))

def getattr_deep(obj, field):
    for f in field.split('.'):
        obj = getattr(obj, f)
    return obj

def reseed(seed=90210, gpu_id=None):
    random.seed(seed)
    torch.manual_seed(seed)
    if gpu_id is not None:
        torch.cuda.manual_seed(seed)
    np.random.seed(seed)

def break_ties_by_policy(reference, policy, state, force_advance_policy=True):
    costs = torch.zeros(state.n_actions)
    try:
        reference.set_min_costs_to_go(state, costs)
    except NotImplementedError:
        ref = reference(state)
        if force_advance_policy:
            policy(state)
        return ref
    # otherwise we successfully got costs
    old_actions = state.actions
    min_cost = min((costs[a] for a in old_actions))
    state.actions = [a for a in old_actions if costs[a] <= min_cost]
    a = policy(state)  # advances policy
    #print costs, old_actions, state.actions, a
    #a = state.actions[0]
    assert a is not None, 'got action None in %s, costs=%s, old_actions=%s' % (state.actions, costs, old_actions)
    state.actions = old_actions
    return a
    

def evaluate(data, policy, losses, verbose=False):
    "Compute average `loss()` of `policy` on `data`"
    was_list = True
    if not isinstance(losses, list):
        losses = [losses]
        was_list = False
    for loss in losses:
        loss.reset()
    for example in data:
        env = example.mk_env()
        res = env.run_episode(policy)
        if verbose:
            print(res, example)
        for loss in losses:
            loss(example, env)
    scores = [loss.get() for loss in losses]
    if not was_list:
        scores = scores[0]
    return scores

def next_print(print_freq, N):
    if print_freq is None:
        return None
    if N < 1:
        return 1
    return N + print_freq if isinstance(print_freq, int) else N * print_freq

def minibatch(data, minibatch_size, reshuffle):
    """
    >>> list(minibatch(range(8), 3, 0))
    [[0, 1, 2], [3, 4, 5], [6, 7]]

    >>> list(minibatch(range(0), 3, 0))
    []
    """
    # TODO this can prob be made way more efficient
    if reshuffle:
        random.shuffle(data)
    mb = []
    data = iter(data)
    try:
        prev_x = next(data)
    except StopIteration:
        # there are no examples
        return
    while True:
        mb.append(prev_x)
        try:
            prev_x = next(data)
        except StopIteration:
            break
        if len(mb) >= minibatch_size:
            yield mb, False
            mb = []
    if len(mb) > 0:
        yield mb, True

def padto(s, l, right=False):
    if isinstance(s, list):
        s = ' '.join(map(str, s))
    elif not isinstance(s, str):
        s = str(s)
    n = len(s)
    if l is not None and n > l:
        return s[:l-2] + '..'
    if l is not None:
        if right:
            s = ' ' * (l - n) + s
        else:
            s += ' ' * (l - n)
    return s

class LearnerToAlg(macarico.LearningAlg):
    def __init__(self, learner, policy, loss):
        macarico.LearningAlg.__init__(self)
        self.learner = learner
        self.policy = policy
        self.loss = loss()

    def __call__(self, example):
        env = example.mk_env()
        env.run_episode(self.learner)
        loss = self.loss.evaluate(example, env)
        obj = self.learner.update(loss)
        return obj, env

class LossMatrix(object):
    def __init__(self, n_ex, losses):
        if not isinstance(losses, list):
            losses = [losses]
        self.losses = [loss() for loss in losses]
        self.A = torch.zeros(3, len(losses)+2)
        self.i = 0
        self.cur_count = 0
        for loss in self.losses:
            loss.reset()
        self.n_ex = n_ex
        self.examples = []

    def append(self, example, env):
        self.cur_count += 1
        for loss in self.losses:
            loss(example, env)
        if len(self.examples) < self.n_ex:
            self.examples.append((example, env.input_x(), env.output()))
        elif np.random.random() < self.n_ex/(self.n_ex+self.cur_count):
            self.examples[np.random.randint(0,self.n_ex)] = (example, env.input_x(), env.output())

    def append_run(self, example, policy):
        env = example.mk_env()
        out = env.run_episode(policy)
        self.append(example, env)
        return out

    def names(self):
        return [loss.name for loss in self.losses]
            
    def next(self, n_ex, epoch):
        M, N = self.A.shape
        if self.i >= M:
            B = torch.zeros(self.i*2, N)
            B[:self.i,:] = self.A
            self.A = B
        for n, loss in enumerate(self.losses):
            self.A[self.i, n] = loss.get()
            loss.reset()
        self.A[self.i,-2] = n_ex
        self.A[self.i,-1] = epoch
        self.i += 1
        self.cur_count = 0
        return self.row(self.i-1)

    def row(self, i):
        assert 0 <= i and i < self.i
        return self.A[i,:-2]

    def last_row(self):
        return self.row(self.i-1)

    def col(self, n):
        assert 0 <= n and n < len(self.losses)
        return self.A[:,n]

class ShortFormatter(object):
    def __init__(self, has_dev, losses, ex_width=20):
        self.start_time = time.time()
        self.has_dev = has_dev
        self.ex_width = ex_width
        self.loss_names = [loss().name for loss in losses]
        self.fmt = '%10.6f  %10.6f' + ('  %10.6f' if has_dev else '') + '  %8s  %5s'
        self.fmt += '  [%s]  [%s]'
        self.fmt += '  %8.2f'
        self.last_N = 0
        extra_loss_header = '    ex/sec'
        if len(losses) > 1:
            #if self.has_dev: self.fmt += ' '
            for name in self.loss_names[1:]:
                extra_loss_header += padto('  tr_' + name, 10, right=True)
                self.fmt += '  %8.5f'
                if has_dev:
                    extra_loss_header += padto('  de_' + name, 10, right=True)
                    self.fmt += '  %8.5f'
                
        self.header = '%s %s%s  %8s  %5s%s%s' % \
              (padto(' objective', 11),
               padto('tr_' + self.loss_names[0], 10, right=True),
               ('  ' + padto(' de_' + self.loss_names[0], 10, right=True)) if has_dev else '',
               'N', 'epoch',
               '  ' + padto('rand_' + ('de' if has_dev else 'tr') + '_truth', ex_width+2) +
               '  ' + padto('rand_' + ('de' if has_dev else 'tr') + '_pred',  ex_width+2),
               extra_loss_header)

    def __call__(self, obj, tr_mat, de_mat, N, epoch, is_best):
        now = time.time()
        tr_err = tr_mat.last_row()
        de_err = de_mat.last_row()
        vals = [obj, tr_err[0]]
        if self.has_dev: vals.append(de_err[0])
        vals += [N, epoch]
        examples = de_mat.examples if self.has_dev else tr_mat.examples
        vals += [padto(examples[0][0], self.ex_width),
                 padto(examples[0][2], self.ex_width)]
        vals.append((N - self.last_N) / (now - self.start_time))
        self.last_N = N
        self.start_time = now
        for i in range(1, len(self.loss_names)):
            vals.append(tr_err[i])
            if self.has_dev: vals.append(de_err[i])
        #import ipdb; ipdb.set_trace()
        s = self.fmt % tuple(vals)
        if is_best: s += ' *'
        return s

class LongFormatter(object):
    def __init__(self, has_dev, losses, ex_width=None):
        self.start_time = time.time()
        self.has_dev = has_dev
        self.ex_width = ex_width
        self.loss_names = [loss().name for loss in losses]
        self.header = None
        self.last_N = 0

    def __call__(self, obj, tr_mat, de_mat, N, epoch, is_best):
        now = time.time()
        tr_err = tr_mat.last_row()
        de_err = de_mat.last_row()
        s = ''
        s += '-' * 80 + '\n'
        s += '  example %11s%s\n    epoch %11s\nobjective  %10.6f\n   ex/sec  %10.6f' % (N, ' *' if is_best else '', epoch, obj, (N - self.last_N) / (now - self.start_time))
        self.start_time = now
        self.last_N = N
        s += '\n'
        s += '    train'
        for i in range(len(self.loss_names)):
            if i > 0: s += '  |'
            s += '  %10.6f %s' % (tr_err[i], self.loss_names[i])
        s += '\n'
        if self.has_dev:
            s += '      dev'
            for i in range(len(self.loss_names)):
                if i > 0: s += '  |'
                s += '  %10.6f %s' % (de_err[i], self.loss_names[i])
            s += '\n'

        s += '\nTRAIN EXAMPLES\n'
        for i, (ex, inp, out) in enumerate(tr_mat.examples):
            ii = ' ' * max(0, 3+len(str(len(tr_mat.examples)-1))-len(str(i)))
            if inp is not None:
                s += ii + 'input%d  %s\n' % (i, padto(inp, None))
            s += ii + 'truth%d  %s\n' % (i, padto(ex, None))
            s += ii + ' pred%d  %s\n' % (i, padto(out, None))
            s += '\n'
            
        if self.has_dev:
            s += '\nDEV EXAMPLES\n'
            for i, (ex, inp, out) in enumerate(de_mat.examples):
                ii = ' ' * max(0, 3+len(str(len(de_mat.examples)-1))-len(str(i)))
                if inp is not None:
                    s += ii + 'input%d  %s\n' % (i, padto(inp, None))
                s += ii + 'truth%d  %s\n' % (i, padto(ex, None))
                s += ii + ' pred%d  %s\n' % (i, padto(out, None))
                s += '\n'
        return s
              
        
    
def trainloop(training_data,
              dev_data=None,
              policy=None,
              learner=None,
              optimizer=None,
              losses=None,      # one or more losses, first is used for early stopping
              n_epochs=10,
              minibatch_size=1,
              run_per_batch=[],
              run_per_epoch=[],
              print_freq=2.0,   # int=additive, float=multiplicative
              print_per_epoch=True,
              quiet=False,
              reshuffle=True,
              returned_parameters='best',  # { best, last, none }
              save_best_model_to=None,
              bandit_evaluation=False,
              n_random_dev=5,
              n_random_train=5,
              formatter=ShortFormatter,
              progress_bar=True,
             ):

    assert learner is not None, \
        'trainloop expects a learner'

    assert losses is not None, \
        'must specify at least one loss function'

    if not isinstance(losses, list):
        losses = [losses]
    
    if bandit_evaluation and n_epochs > 1 and not quiet:
        print('warning: running bandit mode with n_epochs>1, this is weird!',
              file=sys.stderr)

    if dev_data is not None and len(dev_data) == 0:
        dev_data = None

    max_n_eval_train = 50
    tr_loss_matrix = LossMatrix(n_random_train, losses)
    de_loss_matrix = LossMatrix(n_random_dev, losses)

    learning_alg = learner if isinstance(learner, macarico.LearningAlg) else \
                   LearnerToAlg(learner, policy, losses[0])

    formatter = formatter(dev_data is not None, losses)
    if formatter.header is not None:
        print(formatter.header, file=sys.stderr)

    last_print = None
    best_de_err = float('inf')
    final_parameters = None
    error_history = []

    objective_average = Averaging()

    not_streaming = isinstance(training_data, list)
    n_training_ex = len(training_data) if not_streaming else None

    N = 0  # total number of examples seen
    N_print = next_print(print_freq, N)
    N_last = 0
    if progress_bar:
        bar = progressbar.ProgressBar(max_value=int(N_print))
    for epoch in range(1, n_epochs+1):
        M = 0  # total number of examples seen this epoch
        random_train = []
        for batch, is_last_batch in minibatch(training_data, minibatch_size, reshuffle):
            if optimizer is not None:
                optimizer.zero_grad()
            # TODO: minibatching is really only useful if we can
            # preprocess in a useful way

            # when we don't know n_training_ex, we'll just be optimistic that there are
            # still >= max_n_eval_train remaining, which may cause one of the printouts to be
            # erroneous; we can correct for this later in principle if we must
            tr_eval_threshold = N_print
            if is_last_batch and n_training_ex is None:
                n_training_ex = N + len(batch)
            if n_training_ex is not None:
                tr_eval_threshold = min(tr_eval_threshold, n_training_ex)
            tr_eval_threshold -= max_n_eval_train
            
            for example in batch:
                N += 1
                M += 1
                if progress_bar and N <= N_print:
                    bar.update(N-N_last)
                    
                if not bandit_evaluation and N > tr_eval_threshold:
                    tr_loss_matrix.append_run(example, policy)
                    
                opt, final_env = learning_alg(example)
                if bandit_evaluation:
                    tr_loss_matrix.append(example, final_env)
                    
                objective_average.update(opt)

            if optimizer is not None:
                optimizer.step()
                
            if (N_print is not None and N >= N_print) or \
               (is_last_batch and (print_per_epoch or (epoch==n_epochs))):
                update_bar = progress_bar
                N_last = int(N_print)
                N_print = next_print(print_freq, N_print)
                if dev_data is not None:
                    # TODO minibatch this
                    for example in dev_data[:N]:
                        de_loss_matrix.append_run(example, policy)
                
                tr_err = tr_loss_matrix.next(N, epoch)
                de_err = de_loss_matrix.next(N, epoch)

                #import ipdb; ipdb.set_trace()
                #extra_loss_scores = list(itertools.chain(*zip(tr_err[1:], de_err[1:])))

                is_best = de_err[0] < best_de_err
                if progress_bar:
                    sys.stderr.write('\r' + ' ' * (bar.term_width) + '\r')
                    sys.stderr.flush()
                    #bar.finish()
                    
                print(formatter(objective_average(), tr_loss_matrix,
                                de_loss_matrix, N, epoch, is_best),
                      file=sys.stderr)
                objective_average.reset()

                last_print = N
                if is_best:
                    best_de_err = de_err[0]
                    if save_best_model_to is not None:
                        if not quiet:
                            print('saving model to %s...' % save_best_model_to, file=sys.stderr, end='')
                        torch.save(policy.state_dict(), save_best_model_to)
                        if not quiet:
                            sys.stderr.write('\r' + (' ' * (21 + len(save_best_model_to))) + '\r')
                    if returned_parameters == 'best':
                        final_parameters = deepcopy(policy.state_dict())

            if update_bar:
                update_bar = False
                bar = progressbar.ProgressBar(max_value=int(N_print-N_last))

                        
            for x in run_per_batch: x()
        for x in run_per_epoch: x()
        if n_training_ex is None:
            n_training_ex = N
        

    if returned_parameters == 'last':
        final_parameters = deepcopy(policy.state_dict())

    return error_history, final_parameters

########################################################
# synthetic data construction

def make_sequence_reversal_data(num_ex, ex_len, n_types):
    data = []
    for _ in range(num_ex):
        x = [random.choice(range(n_types)) for _ in range(ex_len)]
        y = list(reversed(x))
        data.append((x,y))
    return data

def make_sequence_mod_data(num_ex, ex_len, n_types, n_labels):
    data = []
    for _ in range(num_ex):
        x = np.random.randint(n_types, size=ex_len)
        y = (x+1) % n_labels
        data.append((x,y))
    return data

def test_reference_on(ref, loss, ex, verbose=True, test_values=False, except_on_failure=True):
    from macarico import Policy
    from macarico.policies.linear import LinearPolicy

    env = ex.mk_env()
    policy = LinearPolicy(None, env.n_actions)

    def run(run_strategy):
        env.rewind()
        runner = EpisodeRunner(policy, run_strategy, ref)
        env.run_episode(runner)
        cost = loss()(ex, env)
        return cost, runner.trajectory, runner.limited_actions, runner.costs, runner.ref_costs

    # generate the backbone by REF
    loss0, traj0, limit0, costs0, refcosts0 = run(lambda t: EpisodeRunner.REF)
    if verbose:
        print('loss0', loss0, 'traj0', traj0)

    backbone = lambda t: (EpisodeRunner.ACT, traj0[t])
    n_actions = env.n_actions
    any_fail = False
    for t in range(len(traj0)):
        costs = torch.zeros(n_actions)
        traj1_all = [None] * n_actions
        for a in limit0[t]:
            #if a == traj0[t]: continue
            l, traj1, _, _, _ = run(one_step_deviation(len(traj0), backbone, lambda _: EpisodeRunner.REF, t, a))
            #if verbose:
            #    print t, a, l
            costs[a] = l
            traj1_all[a] = traj1
            if l < loss0 or (a == traj0[t] and l != loss0):
                print('local opt failure, ref loss=%g, loss=%g on deviation (%d, %d), traj0=%s traj\'=%s [ontraj=%s, is_proj=%s]' % \
                    (loss0, l, t, a, traj0, traj1, a == traj0[t], not ex.is_non_projective))
                any_fail = True
                if except_on_failure:
                    raise Exception()
        if test_values:
            for a in limit0[t]:
                if refcosts0[t][a] != costs[a]:
                    print('cost failure, t=%d, a=%d, traj0=%s, traj1=%s, ref_costs=%s, observed costs=%s [is_proj=%s]' % \
                        (t, a, traj0, traj1_all[a], \
                         [refcosts0[t][a0] for a0 in limit0[t]], \
                         [costs[a0] for a0 in limit0[t]], \
                         not ex.is_non_projective))
                    if except_on_failure:
                        raise Exception()

    if not any_fail:
        print('passed!')

def test_reference(ref, loss, data, verbose=False, test_values=False, except_on_failure=True):
    for n, ex in enumerate(data):
        print('# example %d ' % n,)
        test_reference_on(ref, loss, ex, verbose, test_values, except_on_failure)

def sample_action_from_probs(r, probs):
    r0 = r
    for i, v in enumerate(probs):
        r -= v
        if r <= 0:
            return i
    _, mx = probs.max(0)
    print('warning: sampling from %s failed! returning max item %d; (r=%g r0=%g sum=%g)' % \
          (str(np_probs), mx, r, r0, np_probs.sum()), file=sys.stderr)
    return len(np_probs)-1

def sample_from_np_probs(np_probs):
    r = np.random.rand()
    a = sample_action_from_probs(r, np_probs)
    return a, np_probs[a]

def sample_from_probs(probs):
    r = np.random.rand()
    a = sample_action_from_probs(r, probs.data)
    return a, probs[a]
