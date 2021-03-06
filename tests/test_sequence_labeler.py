from __future__ import division, generators, print_function
import random
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.autograd import Variable as Var
import numpy as np
import macarico.util
#macarico.util.reseed()

from macarico.annealing import ExponentialAnnealing, stochastic
from macarico.lts.maximum_likelihood import MaximumLikelihood
from macarico.lts.reinforce import Reinforce
from macarico.lts.dagger import DAgger, TwistedDAgger
from macarico.lts.lols import BanditLOLS
from macarico.annealing import EWMA
from macarico.tasks.sequence_labeler import Example, HammingLoss, HammingLossReference
from macarico.features.sequence import RNNFeatures, BOWFeatures, DilatedCNNFeatures, AttendAt
from macarico.features.actor import TransitionRNN, TransitionBOW
from macarico.policies.linear import LinearPolicy

class LearnerOpts:
    AC = 'ActorCritic'
    DAGGER = 'DAgger'
    REINFORCE = 'REINFORCE'
    BANDITLOLS = 'BanditLOLS'
    TWISTED = 'TwistedDAgger'
    MAXLIK = 'MaximumLikelihood'

Actor = TransitionRNN
#Actor = TransitionBOW

def make_matti_data(count, length, n_types, noise_rate):
    def make_example():
        flip = False
        x = np.random.randint(0, n_types, length)
        y = [0] * length
        y[0] = x[0] % 2
        for i in xrange(1, length):
            y[i] = (x[i] % 2) ^ y[i-1]
            if flip:
                x[i] = n_types - x[i] - 1
            if np.random.random() < noise_rate:
                #x[i] = (1 + x[i]) % n_types
                y[i] = 1 - y[i]
                flip = not flip
        return x, y
    return [make_example() for _ in xrange(count)]

def test0():
    print
    print '# test sequence labeler on mod data with DAgger'
    n_types = 10
    n_labels = 4

    data = [Example(x, y, n_labels) for x, y in macarico.util.make_sequence_mod_data(100, 5, n_types, n_labels)]


    
    tRNN = Actor(
                 [RNNFeatures(
                              n_types,
                              output_field = 'mytok_rnn')],
                 [AttendAt(field='mytok_rnn')],
                 n_labels)
    policy = LinearPolicy(tRNN, n_labels)

    p_rollin_ref = stochastic(ExponentialAnnealing(0.99))
    optimizer = torch.optim.Adam(policy.parameters(), lr=0.01)

    macarico.util.trainloop(
        training_data   = data[:len(data)//2],
        dev_data        = data[len(data)//2:],
        policy          = policy,
        Learner         = lambda: DAgger(HammingLossReference(), policy, p_rollin_ref),
        losses          = HammingLoss(),
        optimizer       = optimizer,
        run_per_epoch   = [p_rollin_ref.step],
        n_epochs        = 4,
        train_eval_skip = 1,
    )


def test1(task=0, LEARNER=LearnerOpts.DAGGER):
    print
    print 'Running test 1 (v%d) with learner=%s' % (task, LEARNER)
    print '======================================================='

    if task == 0:
        print 'Sequence reversal task, easy version'
        data = macarico.util.make_sequence_reversal_data(100, 5, 5)
        foci = [AttendAt(lambda s: s.N-s.n-1)]
    elif task == 1:
        print 'Sequence reversal task, hard version'
        data = macarico.util.make_sequence_reversal_data(1000, 5, 5)
        foci = [AttendAt()]
    elif task == 2:
        print 'Sequence reversal task, multi-focus version'
        data = macarico.util.make_sequence_reversal_data(100, 5, 5)
        foci = [AttendAt(), AttendAt(lambda s: s.N-s.n-1)]
    elif task == 3:
        print 'Memoryless task, add-one mod K'
        data = macarico.util.make_sequence_mod_data(50, 5, 10, 3)
        foci = [AttendAt()]
    elif task == 4:
        print 'Matti-style data'
        data = make_matti_data(1000, 20, 2, 0.05)
        foci = [AttendAt()]


    n_types = 1+max({x for X, _ in data for x in X})
    n_labels = 1+max({y for _, Y in data for y in Y})

    data = [Example(x, y, n_labels) for x, y in data]

    random.shuffle(data)
    m = len(data)//2
    train = data[:m]
    dev = data[m:]

    print 'n_train: %s, n_dev: %s' % (len(train), len(dev))
    print 'n_types: %s, n_labels: %s' % (n_types, n_labels)
    print 'learner:', LEARNER
    print


    
    tRNN = Actor([RNNFeatures(n_types)], foci, n_labels)
    policy = LinearPolicy(tRNN, n_labels )

    baseline = EWMA(0.8)
    p_rollin_ref  = stochastic(ExponentialAnnealing(0.5))
    p_rollout_ref = stochastic(ExponentialAnnealing(0.5))

    if LEARNER == LearnerOpts.AC:
        from macarico.lts.reinforce import AdvantageActorCritic, LinearValueFn
        baseline = LinearValueFn(policy.features)
        policy.vfa = baseline   # adds params to policy via nn.module

    optimizer = torch.optim.Adam(policy.parameters(), lr=0.01)

    if LEARNER == LearnerOpts.DAGGER:
        learner = lambda: DAgger(HammingLossReference(), policy, p_rollin_ref)
    elif LEARNER == LearnerOpts.TWISTED:
        learner = lambda: TwistedDAgger(HammingLossReference(), policy, p_rollin_ref)
    elif LEARNER == LearnerOpts.MAXLIK:
        learner = lambda: MaximumLikelihood(HammingLossReference(), policy)
    elif LEARNER == LearnerOpts.AC:
        learner = lambda: AdvantageActorCritic(policy, baseline)
    elif LEARNER == LearnerOpts.REINFORCE:
        learner = lambda: Reinforce(policy, baseline)
    elif LEARNER == LearnerOpts.BANDITLOLS:
        learner = lambda: BanditLOLS(HammingLossReference(),
                                     policy,
                                     p_rollin_ref,
                                     p_rollout_ref,
                                     BanditLOLS.LEARN_DR,
                                     BanditLOLS.EXPLORE_UNIFORM,
                                     baseline)

    macarico.util.trainloop(
        training_data   = train,
        dev_data        = dev,
        policy          = policy,
        Learner         = learner,
        losses          = HammingLoss(),
        optimizer       = optimizer,
        run_per_epoch   = [p_rollin_ref.step, p_rollout_ref.step],
        n_epochs        = 10,
        train_eval_skip = 1,
    )


def test_wsj():
    print
    print '# test on wsj subset'
    from macarico.data import nlp_data
    tr,de,te,vocab,label_id = \
      nlp_data.read_wsj_pos('data/wsj.pos', n_tr=50, n_de=50, n_te=0)

    n_types = len(vocab)
    n_labels = len(label_id)

    print 'n_train: %s, n_dev: %s, n_test: %s' % (len(tr), len(de), len(te))
    print 'n_types: %s, n_labels: %s' % (n_types, n_labels)


    tRNN = TransitionRNN(
                         [RNNFeatures(n_types, rnn_type='RNN')],
                         [AttendAt()],
                         n_labels)
    policy = LinearPolicy(tRNN, n_labels)

    p_rollin_ref = stochastic(ExponentialAnnealing(0.9))
    optimizer = torch.optim.Adam(policy.parameters(), lr=0.01)

    macarico.util.trainloop(
        training_data   = tr,
        dev_data        = de,
        policy          = policy,
        Learner         = lambda: DAgger(HammingLossReference(), policy, p_rollin_ref),
#        Learner         = lambda: MaximumLikelihood(HammingLossReference(), policy),
        losses          = HammingLoss(),
        optimizer       = optimizer,
        run_per_epoch   = [p_rollin_ref.step],
        n_epochs        = 10,
#        train_eval_skip = None,
    )

# TODO: Tim will ressurect the stuff below shortly.
#
#def hash_list(*l):
#    x = 431801
#    for y in l:
#        x = int((x + y) * 849107)
#    return x
#
#
#def noisy_label(y, n_labels, noise_level):
#    if random.random() < noise_level:
#        return random.randint(0,n_labels-1)
#    return y % n_labels
#
#
#def make_xor_data(n_types, n_labels, n_ex, sent_len, history_length, noise_level=0.1):
#    training_data = []
#    for _ in xrange(n_ex):
#        tokens,labels = [],[]
#        tokens.append(random.randint(0,n_types-1))
#        labels.append(noisy_label(tokens[-1], n_labels, noise_level))
#        for _ in xrange(sent_len-1):
#            tokens.append(random.randint(0,n_types-1))
#            hist = hash_list(tokens[-1], *labels[-history_length:])
#            labels.append(noisy_label(hist, n_labels, noise_level))
#        training_data.append((tokens,labels))
#    return training_data
#
#
#def train_test(n_types, n_labels, training_data, dev_data, test_data, n_epochs, batch_size,
#               d_emb, d_rnn, d_actemb, d_hid, lr, mk_lts, mk_lts_args={}):
#    task = SequenceLabeler(n_types,
#                           n_labels,
#                           HammingReference,
#                           d_emb = d_emb,
#                           d_rnn = d_rnn,
#                           d_actemb = d_actemb,
#                           d_hid = d_hid)
#
#    #lts = DAgger(p_rollin_ref=NoAnnealing(1.))
#    lts = mk_lts(**mk_lts_args)
#    optimizer  = optim.Adam(task.parameters(), lr=lr)
#
#    def eval_on(data):
#        err = 0.
#        for tokens,labels in data:
#            torch_tokens = Variable(torch.LongTensor(tokens))
#            pred = task.forward(torch_tokens)  # no labels ==> test mode
#            this_err = sum([a!=b for a,b in zip(pred,labels)])
#            #this_err2 = task.ref_policy.final_loss()
#            #print task.ref_policy.truth, task.ref_policy.prediction
#            #assert this_err2 == this_err, 'mismatch %g != %g' % (this_err, this_err2)
#            #print this_err
#            err += this_err
#        return err / len(data)
#
#    # train
#    best = None
#    for epoch in xrange(n_epochs):
#        obj_value = 0.
#        for n in xrange(0, len(training_data), batch_size):
#            optimizer.zero_grad()
#            lts.zero_objective()
#            for tokens,labels in training_data[n:n+batch_size]:
#                torch_tokens = Variable(torch.LongTensor(tokens))
#                output = task.forward(torch_tokens, labels, lts)
#                lts.backward()
#                #obj = lts.get_objective()
#                #obj_value += obj.data[0]
#                #obj /= batch_size
#                #obj.backward()#retain_variables=True)
##            obj = lts.get_objective()
##            obj_value += obj.data[0]
##            obj /= batch_size
##            obj.backward()#retain_variables=True)
#            optimizer.step()
#        lts.new_pass()
#        if epoch % 10 == 0:
#            [tr,de,te] = map(eval_on, [training_data, dev_data, test_data])
#            if best is None or de < best[0]: best = (de,te)
#            print 'ep %d\ttr %g\tde %g\tte %g\tte* %g\tob %g' % (epoch, tr, de, te, best[1],
#                                                                 obj_value / len(training_data))
#
#
#def test2(n_types = 20,
#          n_labels = 5,
#          n_ex = 100,
#          sent_len = 5,
#          history_length = 1,
#          noise_level = 0.,
#          n_epochs = 1000,
#          batch_size = 1,
#          d_emb = 15,
#          d_rnn = 15,
#          d_actemb = 15,
#          d_hid = 15,
#          lr = 1e-2,
#          lts = MaximumLikelihood,
#          lts_args = {},
#          reseed=True):
#
#    print
#    print 'Running test 2'
#    print '=============='
#    if reseed: re_seed()
#
#    all_data = make_xor_data(n_types, n_labels, n_ex*3,
#                             sent_len, history_length, noise_level)
#
#    training_data = all_data[:n_ex]
#    dev_data      = all_data[n_ex:n_ex*2]
#    test_data     = all_data[2*n_ex:]
#
#    train_test(n_types, n_labels, training_data, dev_data, test_data, n_epochs, batch_size,
#               d_emb, d_rnn, d_actemb, d_hid, lr, lts, lts_args)


if __name__ == '__main__':
    test0()
    for i in xrange(3):
        test1(i, LearnerOpts.MAXLIK)
        test1(i, LearnerOpts.DAGGER)
        #test1(i, LearnerOpts.TWISTED)
    #for l in [LearnerOpts.MAXLIK, LearnerOpts.DAGGER]: #, LearnerOpts.REINFORCE, LearnerOpts.BANDITLOLS, LearnerOpts.AC]:
    #    test1(0, l)
    test_wsj()
