import multiprocessing as mp
if mp.get_start_method(allow_none=True) != 'spawn':
    mp.set_start_method('spawn', force=True)

import argparse
import os
import time
import logging
from datetime import datetime
import numpy as np
import yaml
import pdb

import torch
import torch.nn as nn
import torch.backends.cudnn as cudnn
import torch.optim
from torch.utils.data import DataLoader
from tensorboardX import SummaryWriter

import models
from datasets import FaceDataset, GivenSizeSampler
from utils import AverageMeter, load_state, save_state, log
import test

model_names = sorted(name for name in models.backbones.__dict__
    if name.islower() and not name.startswith("__")
    and callable(models.backbones.__dict__[name]))

class ArgObj(object):
    def __init__(self):
        pass

parser = argparse.ArgumentParser(description='Multi-Task Face Recognition Training')
parser.add_argument('--config', type=str, required=True)
parser.add_argument('--load-path', default='', type=str)
parser.add_argument('--resume', action='store_true')
parser.add_argument('--evaluate', action='store_true')
parser.add_argument('--extract', action='store_true')

def main():

    ## config
    global args
    args = parser.parse_args()

    with open(args.config) as f:
        config = yaml.load(f)

    for k,v in config.items():    
        if isinstance(v, dict):
            argobj = ArgObj()
            setattr(args, k, argobj)
            for kk,vv in v.items():
                setattr(argobj, kk, vv)
        else:
            setattr(args, k, v)
    args.ngpu = len(args.gpus.split(','))

    ## asserts
    assert args.model.backbone in model_names, "available backbone names: {}".format(model_names)
    num_tasks = len(args.train.data_root)
    assert(num_tasks == len(args.train.loss_weight))
    assert(num_tasks == len(args.train.batch_size))
    assert(num_tasks == len(args.train.data_list))
    assert(num_tasks == len(args.train.data_meta))
    if args.val.flag:
        assert(num_tasks == len(args.val.batch_size))
        assert(num_tasks == len(args.val.data_root))
        assert(num_tasks == len(args.val.data_list))
        assert(num_tasks == len(args.val.data_meta))

    ## mkdir
    if not hasattr(args, 'save_path'):
        args.save_path = os.path.dirname(args.config)
    if not os.path.isdir('{}/checkpoints'.format(args.save_path)):
        os.makedirs('{}/checkpoints'.format(args.save_path))
    if not os.path.isdir('{}/logs'.format(args.save_path)):
        os.makedirs('{}/logs'.format(args.save_path))
    if not os.path.isdir('{}/events'.format(args.save_path)):
        os.makedirs('{}/events'.format(args.save_path))

    ## create dataset
    if not (args.extract or args.evaluate): # train + val
        for i in range(num_tasks):
            args.train.batch_size[i] *= args.ngpu

        train_dataset = [FaceDataset(args, idx, 'train') for idx in range(num_tasks)]
        args.num_classes = [td.num_class for td in train_dataset]
        train_longest_size = max([int(np.ceil(len(td) / float(bs))) for td, bs in zip(train_dataset, args.train.batch_size)])
        train_sampler = [GivenSizeSampler(td, total_size=train_longest_size * bs, rand_seed=args.train.rand_seed) for td, bs in zip(train_dataset, args.train.batch_size)]
        train_loader = [DataLoader(
            train_dataset[k], batch_size=args.train.batch_size[k], shuffle=False,
            num_workers=args.workers, pin_memory=False, sampler=train_sampler[k]) for k in range(num_tasks)]
        assert(all([len(train_loader[k]) == len(train_loader[0]) for k in range(num_tasks)]))

        if args.val.flag:
            for i in range(num_tasks):
                args.val.batch_size[i] *= args.ngpu
    
            val_dataset = [FaceDataset(args, idx, 'val') for idx in range(num_tasks)]
            val_longest_size = max([int(np.ceil(len(vd) / float(bs))) for vd, bs in zip(val_dataset, args.val.batch_size)])
            val_sampler = [GivenSizeSampler(vd, total_size=val_longest_size * bs, sequential=True) for vd, bs in zip(val_dataset, args.val.batch_size)]
            val_loader = [DataLoader(
                val_dataset[k], batch_size=args.val.batch_size[k], shuffle=False,
                num_workers=args.workers, pin_memory=False, sampler=val_sampler[k]) for k in range(num_tasks)]
            assert(all([len(val_loader[k]) == len(val_loader[0]) for k in range(num_tasks)]))

    if args.test.flag or args.evaluate: # online or offline evaluate
        args.test.batch_size *= args.ngpu
        test_dataset = FaceDataset(args, 0, 'test')
        test_sampler = GivenSizeSampler(
            test_dataset, total_size=int(np.ceil(len(test_dataset) / float(args.test.batch_size)) * args.test.batch_size), sequential=True)
        test_loader = DataLoader(
            test_dataset, batch_size=args.test.batch_size, shuffle=False,
            num_workers=args.workers, pin_memory=False, sampler=test_sampler)

    if args.extract: # feature extraction
        args.extract_info.batch_size *= args.ngpu
        extract_dataset = FaceDataset(args, 0, 'extract')
        extract_sampler = GivenSizeSampler(
            extract_dataset, total_size=int(np.ceil(len(extract_dataset) / float(args.extract_info.batch_size)) * args.extract_info.batch_size), sequential=True)
        extract_loader = DataLoader(
            extract_dataset, batch_size=args.extract_info.batch_size, shuffle=False,
            num_workers=args.workers, pin_memory=False, sampler=extract_sampler)


    ## create model
    log("Creating model on [{}] gpus: {}".format(args.ngpu, args.gpus))
    if args.evaluate or args.extract:
        args.num_classes = None
    model = models.MultiTaskWithLoss(backbone=args.model.backbone, num_classes=args.num_classes, feature_dim=args.model.feature_dim, spatial_size=args.transform.final_size, arc_fc=args.model.arc_fc, feat_bn=args.model.feat_bn)
    os.environ["CUDA_VISIBLE_DEVICES"] = args.gpus
    model = nn.DataParallel(model)
    model.cuda()
    cudnn.benchmark = True

    ## criterion and optimizer
    optimizer = torch.optim.SGD(model.parameters(), args.train.base_lr,
                                momentum=args.train.momentum,
                                weight_decay=args.train.weight_decay)

    ## resume / load model
    start_epoch = 0
    count = [0]
    if args.load_path:
        assert os.path.isfile(args.load_path), "File not exist: {}".format(args.load_path)
        if args.resume:
            checkpoint = load_state(args.load_path, model, optimizer)
            start_epoch = checkpoint['epoch']
            count[0] = checkpoint['count']
        else:
            load_state(args.load_path, model)

    ## offline evaluate
    if args.evaluate:
        evaluation(test_loader, model, num=len(test_dataset), outfeat_fn="{}_{}.bin".format(args.load_path[:-8], args.test.benchmark), benchmark=args.test.benchmark)
        return

    ## feature extraction
    if args.extract:
        extract(extract_loader, model, num=len(extract_dataset), output_file="{}_{}.bin".format(args.load_path[:-8], args.extract_info.data_name))
        return

    ######################## train #################
    ## lr scheduler
    lr_scheduler = torch.optim.lr_scheduler.MultiStepLR(optimizer, args.train.lr_decay_steps, gamma=args.train.lr_decay_scale, last_epoch=start_epoch-1)

    ## logger
    logging.basicConfig(filename=os.path.join('{}/logs'.format(args.save_path), 'log-{}-{:02d}-{:02d}_{:02d}:{:02d}:{:02d}.txt'.format(
        datetime.today().year, datetime.today().month, datetime.today().day,
        datetime.today().hour, datetime.today().minute, datetime.today().second)),
        level=logging.INFO)
    tb_logger = SummaryWriter('{}/events'.format(args.save_path))

    ## initial validate
    if args.val.flag:
        validate(val_loader, model, start_epoch, args.train.loss_weight, len(train_loader[0]), tb_logger)

    ## initial evaluate
    if args.test.flag and args.test.initial_test:
        log("*************** evaluation epoch [{}] ***************".format(start_epoch))
        res = evaluation(test_loader, model, num=len(test_dataset), outfeat_fn="{}/checkpoints/ckpt_epoch_{}_{}.bin".format(args.save_path, start_epoch, args.test.benchmark), benchmark=args.test.benchmark)
        if args.test.benchmark == "megaface":
            tb_logger.add_scalar('megaface', res, start_epoch)
        elif args.test.benchmark == "ijba":
            tb_logger.add_scalar('ijba', res, start_epoch)
        else:
            raise Exception("No such benchmark: {}".format(args.test.benchmark))

    ## training loop
    for epoch in range(start_epoch, args.train.max_epoch):
        lr_scheduler.step()
        for ts in train_sampler:
            ts.set_epoch(epoch)
        # train for one epoch
        train(train_loader, model, optimizer, epoch, args.train.loss_weight, tb_logger, count)
        # save checkpoint
        save_state({
            'epoch': epoch + 1,
            'arch': args.model.backbone,
            'state_dict': model.state_dict(),
            'optimizer' : optimizer.state_dict(),
            'count': count[0]
        }, args.save_path + "/checkpoints/ckpt_epoch", epoch + 1, is_last=(epoch + 1 == args.train.max_epoch))
        # validate
        if args.val.flag:
            validate(val_loader, model, epoch, args.train.loss_weight, len(train_loader[0]), tb_logger, count)
        # online evaluate
        if args.test.flag and ((epoch + 1) % args.test.interval == 0 or epoch + 1 == args.train.max_epoch):
            log("*************** evaluation epoch [{}] ***************".format(epoch + 1))
            res = evaluation(test_loader, model, num=len(test_dataset), outfeat_fn="{}/checkpoints/ckpt_epoch_{}_{}.bin".format(args.save_path, epoch + 1, args.test.benchmark), benchmark=args.test.benchmark)
            if args.test.benchmark == "megaface":
                tb_logger.add_scalar('megaface', res, epoch + 1)
            elif args.test.benchmark == "ijba":
                tb_logger.add_scalar('ijba', res, epoch + 1)
            else:
                raise Exception("No such benchmark: {}".format(args.test.benchmark))


def train(train_loader, model, optimizer, epoch, loss_weight, tb_logger, count):
    num_tasks = len(train_loader)
    batch_time = AverageMeter(args.train.average_stats)
    data_time = AverageMeter(args.train.average_stats)
    losses = [AverageMeter(args.train.average_stats) for k in range(num_tasks)]

    # switch to train mode
    model.train()

    end = time.time()
    for i, all_in in enumerate(zip(*tuple(train_loader))):
        input, target = zip(*[all_in[k] for k in range(num_tasks)])
        slice_pt = 0
        slice_idx = [0]
        for l in [p.size(0) for p in input]:
            slice_pt += l // args.ngpu
            slice_idx.append(slice_pt)

        organized_input = []
        organized_target = []
        for ng in range(args.ngpu):
            for t in range(len(input)):
                bs = args.train.batch_size[t] // args.ngpu
                organized_input.append(input[t][ng * bs : (ng + 1) * bs, ...])
                organized_target.append(target[t][ng * bs : (ng + 1) * bs, ...])
        input = torch.cat(organized_input, dim=0)
        target = torch.cat(organized_target, dim=0)

        # measure data loading time
        data_time.update(time.time() - end)

        input_var = torch.autograd.Variable(input.cuda())
        target_var = torch.autograd.Variable(target.cuda(async=True))

        # measure accuracy and record loss
        loss = model(input_var, target_var, slice_idx)

        for k in range(num_tasks):
            losses[k].update(loss[k].mean().data[0])

        # compute gradient and do SGD step
        optimizer.zero_grad()
        loss_total = 0.
        for k in range(num_tasks):
            loss_total = loss_total + loss[k].mean() * loss_weight[k]
        loss_total.backward()
        optimizer.step()

        # measure elapsed time
        batch_time.update(time.time() - end)
        end = time.time()

        # info
        if i % args.train.print_freq == 0:
            log('Progress: [{0}][{1}/{2}][{3}]    '
                  'Lr: {4:.2g}    '
                  'Time {batch_time.val:.3f} ({batch_time.avg:.3f})    '
                  'Data {data_time.val:.3f} ({data_time.avg:.3f})'.format(
                   epoch, i, len(train_loader[0]), count[0], 
                   optimizer.param_groups[0]['lr'],
                   batch_time=batch_time,
                   data_time=data_time))
            for k in range(num_tasks):
                log('Task: #{0}\t'
                      'LW: {1:.2g}\t'
                      'Loss {loss.val:.4f} ({loss.avg:.4f})'.format(
                       k, loss_weight[k], loss=losses[k]))

        # tensorboard logger
        for k in range(num_tasks):
            tb_logger.add_scalar('train_loss_{}'.format(k), losses[k].val, count[0])
        tb_logger.add_scalar('lr', optimizer.param_groups[0]['lr'], count[0])

        count[0] += 1

def validate(val_loader, model, criterion, epoch, loss_weight, train_len, tb_logger, count):
    raise NotImplemented
    num_tasks = len(val_loader)
    losses = [AverageMeter(args.val.average_stats) for k in range(num_tasks)]

    # switch to evaluate mode
    model.eval()

    start = time.time()
    for i, all_in in enumerate(zip(*tuple(val_loader))):
        input, target = zip(*[all_in[k] for k in range(num_tasks)])

        slice_pt = 0
        slice_idx = [0]
        for l in [p.size(0) for p in input]:
            slice_pt += l
            slice_idx.append(slice_pt)

        input = torch.cat(tuple(input), dim=0)

        target = [tg.cuda(async=True) for tg in target]
        input_var = torch.autograd.Variable(input.cuda(), volatile=True)
        target_var = [torch.autograd.Variable(tg, volatile=True) for tg in target]

        # measure accuracy and record loss
        loss = model(input_var, target_var, slice_idx)

        for k in range(num_tasks):
            losses[k].update(loss[k].data[0])

    log('Test epoch #{}    Time {}'.format(epoch, time.time() - start))
    for k in range(num_tasks):
        log(' * Task: #{0}    Loss {loss.avg:.4f}'.format(k, loss=losses[k]))

    for k in range(num_tasks):
        tb_logger.add_scalar('val_loss_{}'.format(k), losses[k].val, count[0])

def extract(ext_loader, model, num, output_file):
    batch_time = AverageMeter(9999999)
    data_time = AverageMeter(9999999)
    model.eval()
    features = []

    start = time.time()
    end = time.time()
    for i, (input, _) in enumerate(ext_loader):
        data_time.update(time.time() - end)
        input_var = torch.autograd.Variable(input.cuda(), volatile=True)
        output = model(input_var, extract_mode=True)
        features.append(output.data.cpu().numpy())
        batch_time.update(time.time() - end)
        end = time.time()

        if i % args.train.print_freq == 0:
            log("Extracting: {0}/{1}\t"
                    "Time {batch_time.val:.3f} ({batch_time.avg:.3f})\t"
                    "Data {data_time.val:.3f} ({data_time.avg:.3f})".format(
                    i, len(ext_loader), batch_time=batch_time, data_time=data_time))

    features = np.concatenate(features, axis=0)[:num, :]
    features.tofile(output_file)
    log("Extracting Done. Total time: {}".format(time.time() - start))
    return features

def evaluation(test_loader, model, num, outfeat_fn, benchmark):
    load_feat = False
    if not os.path.isfile(outfeat_fn) or not load_feat:
        features = extract(test_loader, model, num, outfeat_fn)
    else:
        log("Loading features: {}".format(outfeat_fn))
        features = np.fromfile(outfeat_fn, dtype=np.float32).reshape(-1, args.model.feature_dim)

    if benchmark == "megaface":
        r = test.test_megaface(features)
        log(' * Megaface: 1e-6 [{}], 1e-5 [{}], 1e-4 [{}]'.format(r[-1], r[-2], r[-3]))
        with open(outfeat_fn[:-4] + ".txt", 'w') as f:
            f.write(' * Megaface: 1e-6 [{}], 1e-5 [{}], 1e-4 [{}]'.format(r[-1], r[-2], r[-3]))
        return r[-1]
    elif benchmark == "ijba":
        r = test.test_ijba(features)
        log(' * IJB-A: {} [{}], {} [{}], {} [{}]'.format(r[0][0], r[0][1], r[1][0], r[1][1], r[2][0], r[2][1]))
        with open(outfeat_fn[:-4] + ".txt", 'w') as f:
            f.write(' * IJB-A: {} [{}], {} [{}], {} [{}]'.format(r[0][0], r[0][1], r[1][0], r[1][1], r[2][0], r[2][1]))
        return r[2][1]
    elif benchmark == "lfw":
        r = test.test_lfw(features)
        log(' * LFW: mean: {} std: {}'.format(r[0], r[1]))
        with open(outfeat_fn[:-4] + ".txt", 'w') as f:
            f.write(' * LFW: mean: {} std: {}'.format(r[0], r[1]))
        return r[0]


if __name__ == '__main__':
    main()
