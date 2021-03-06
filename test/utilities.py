from __future__ import print_function
import argparse
import numpy as np
import os

from collections import defaultdict
from multiprocessing import Process
from sklearn.metrics import auc

from atm.config import *
from atm.worker import work
from atm.database import db_session
from atm.utilities import download_file_http

try:
    import matplotlib.pyplot as plt
except ImportError:
    plt = None

BASELINE_PATH = 'test/baselines/best_so_far/'
DATA_URL = 'https://s3.amazonaws.com/mit-dai-delphi-datastore/downloaded/'
BASELINE_URL = 'https://s3.amazonaws.com/mit-dai-delphi-datastore/best_so_far/'


def get_best_so_far(db, datarun_id):
    """
    Get a series representing best-so-far performance for datarun_id.
    """
    # generate a list of the "best so far" score after each classifier was
    # computed (in chronological order)
    classifiers = db.get_classifiers(datarun_id=datarun_id)
    y = []
    for l in classifiers:
        best_so_far = max(y + [l.cv_judgment_metric])
        y.append(best_so_far)
    return y


def graph_series(length, title, **series):
    """
    Graph series of performance metrics against one another.

    length: all series will be truncated to this length
    title: what to title the graph
    **series: mapping of labels to series of performance data
    """
    if plt is None:
        raise ImportError("Unable to import matplotlib")

    lines = []
    for label, data in series.items():
        # copy up to `length` of the values in `series` into y.
        y = data[:length]
        x = range(len(y))

        # plot y against x
        line, = plt.plot(x, y, '-', label=label)
        lines.append(line)

    plt.xlabel('classifiers')
    plt.ylabel('performance')
    plt.title(title)
    plt.legend(handles=lines)
    plt.show()


def report_auc_vs_baseline(db, rid, graph=False):
    with db_session(db):
        run = db.get_datarun(rid)
        ds = run.dataset
        test = [float(y) for y in get_best_so_far(db, rid)]

    ds_file = os.path.basename(ds.train_path)
    bl_path = download_file_http(BASELINE_URL + ds_file,
                                 local_folder=BASELINE_PATH)

    with open(bl_path) as f:
        baseline = [float(l.strip()) for l in f]

    min_len = min(len(baseline), len(test))
    x = range(min_len)

    test_auc = auc(x, test[:min_len])
    bl_auc = auc(x, baseline[:min_len])
    diff = test_auc - bl_auc

    print('Dataset %s (datarun %d)' % (ds_file, rid))
    print('AUC: test = %.3f, baseline = %.3f (%.3f)' % (test_auc, bl_auc, diff))

    if graph:
        graph_series(100, ds_file, baseline=baseline, test=test)

    return test_auc, bl_auc


def print_summary(db, rid):
    run = db.get_datarun(rid)
    ds = db.get_dataset(run.dataset_id)
    print()
    print('Dataset %s' % ds)
    print('Datarun %s' % run)

    classifiers = db.get_classifiers(datarun_id=rid)
    errs = db.get_classifiers(datarun_id=rid, status=ClassifierStatus.ERRORED)
    complete = db.get_classifiers(datarun_id=rid,
                                  status=ClassifierStatus.COMPLETE)
    print('Classifiers: %d total; %d errors, %d complete' %
          (len(classifiers), len(errs), len(complete)))

    best = db.get_best_classifier(score_target=run.score_target,
                                  datarun_id=run.id)
    if best is not None:
        score = best.cv_judgment_metric
        err = 2 * best.cv_judgment_metric_stdev
        print('Best result overall: classifier %d, %s = %.3f +- %.3f' %\
            (best.id, run.metric, score, err))


def print_method_summary(db, rid):
    # maps methods to sets of hyperpartitions, and hyperpartitions to lists of
    # classifiers
    alg_map = {a: defaultdict(list) for a in db.get_methods(datarun_id=rid)}

    run = db.get_datarun(rid)
    classifiers = db.get_classifiers(datarun_id=rid)
    for l in classifiers:
        hp = db.get_hyperpartition(l.hyperpartition_id)
        alg_map[hp.method][hp.id].append(l)

    for alg, hp_map in alg_map.items():
        print()
        print('method %s:' % alg)

        classifiers = sum(hp_map.values(), [])
        errored = len([l for l in classifiers if l.status ==
                       ClassifierStatus.ERRORED])
        complete = len([l for l in classifiers if l.status ==
                        ClassifierStatus.COMPLETE])
        print('\t%d errored, %d complete' % (errored, complete))

        best = db.get_best_classifier(score_target=run.score_target,
                                      datarun_id=rid, method=alg)
        if best is not None:
            score = best.cv_judgment_metric
            err = 2 * best.cv_judgment_metric_stdev
            print('\tBest: classifier %s, %s = %.3f +- %.3f' % (best, run.metric,
                                                                score, err))

def print_hp_summary(db, rid):
    run = db.get_datarun(rid)
    classifiers = db.get_classifiers(datarun_id=rid)

    part_map = defaultdict(list)
    for c in classifiers:
        hp = c.hyperpartition_id
        part_map[hp].append(c)

    for hp, classifiers in part_map.items():
        print()
        print('hyperpartition', hp)
        print(db.get_hyperpartition(hp))

        errored = len([c for c in classifiers if c.status ==
                       ClassifierStatus.ERRORED])
        complete = len([c for c in classifiers if c.status ==
                        ClassifierStatus.COMPLETE])
        print('\t%d errored, %d complete' % (errored, complete))

        best = db.get_best_classifier(score_target=run.score_target,
                                      datarun_id=rid, hyperpartition_id=hp)
        if best is not None:
            score = best.cv_judgment_metric
            err = 2 * best.cv_judgment_metric_stdev
            print('\tBest: classifier %s, %s = %.3f +- %.3f' % (best, run.metric,
                                                                score, err))

def work_parallel(db, datarun_ids=None, aws_config=None, n_procs=4):
    print('starting workers...')
    kwargs = dict(db=db, datarun_ids=datarun_ids, save_files=False,
                  choose_randomly=True, cloud_mode=False,
                  aws_config=aws_config, wait=False)

    if n_procs > 1:
        # spawn a set of worker processes to work on the dataruns
        procs = []
        for i in range(n_procs):
            p = Process(target=work, kwargs=kwargs)
            p.start()
            procs.append(p)

        # wait for them to finish
        for p in procs:
            p.join()
    else:
        work(**kwargs)
