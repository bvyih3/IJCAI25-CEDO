import os
import sys
import json
import argparse
from pprint import pprint

import torch
import torch.nn as nn
import torch.backends.cudnn as cudnn
from torch.utils.data import DataLoader
from tensorboardX import SummaryWriter

import utils.utils as utils
import utils.config as config
from train import train, evaluate
import modules.base_model_arcface as base_model
from utils.dataset import Dictionary, VQAFeatureDataset
from utils.losses import Plain
from modules.gms import GMS

def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('--epochs', type=int, default=40,
                        help='number of running epochs')
    parser.add_argument('--lr', type=float, default=0.002,
                        help='learning rate for adamax')
    parser.add_argument('--lrq', type=float, default=0.002,
                        help='learning rate for adamax of question modality')
    parser.add_argument('--lrv', type=float, default=0.003,
                        help='learning rate for adamax of image modality')
    parser.add_argument('--lrf', type=float, default=0.003,
                        help='learning rate for adamax of fusion modality')
    parser.add_argument('--loss-fn', type=str, default='Plain',
                        help='chosen loss function')
    parser.add_argument('--num-hid', type=int, default=1024,
                        help='number of dimension in last layer')
    parser.add_argument('--model', type=str, default='baseline_newatt',
                        help='model structure')
    parser.add_argument('--name', type=str, default='exp0.pth',
                        help='saved model name')
    parser.add_argument('--name-new', type=str, default=None,
                        help='combine with fine-tune')
    parser.add_argument('--batch-size', type=int, default=512,
                        help='training batch size')
    parser.add_argument('--fine-tune', action='store_true',
                        help='fine tuning with our loss')
    parser.add_argument('--resume', action='store_true',
                        help='whether resume from checkpoint')
    parser.add_argument('--not-save', action='store_true',
                        help='do not overwrite the old model')
    parser.add_argument('--test', dest='test_only', action='store_true',
                        help='test one time')
    parser.add_argument('--eval-only', action='store_true',
                        help='evaluate on the val set one time')
    parser.add_argument("--gpu", type=str, default='0',
                        help='gpu card ID')
    parser.add_argument('--baseline', action='store_true')
    parser.add_argument('--DLR', action='store_true')
    parser.add_argument('--GMS', action='store_true')
    parser.add_argument('--MHO', action='store_true')
    parser.add_argument(
        "--dataset",
        default="slake",
        choices=["slake", "slake-cp", "vqa-rad", "vqa-rad-cp", "vqace"],
        help="choose dataset",
    )
    args = parser.parse_args()
    return args


if __name__ == '__main__':
    args = parse_args()
    dataset = args.dataset
    config.dataset = dataset
    config.update_paths(args.dataset)
    if not args.MHO:
        args.lrv = args.lr
        args.lrf = args.lr
    if dataset in ['vqa-rad', 'vqa-rad-cp', 'slake', 'slake-cp']:
        args.batch_size = 64
    print(args)

    os.environ["CUDA_VISIBLE_DEVICES"] = args.gpu
    cudnn.benchmark = True

    seed = 1111
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    torch.backends.cudnn.benchmark = True

    if 'log' not in args.name:
        args.name = 'logs/' + args.name
    if args.test_only or args.fine_tune or args.eval_only:
        args.resume = True
    if args.resume and not args.name:
        raise ValueError("Resuming requires folder name!")
    if args.resume:
        logs = torch.load(args.name)
        print("loading logs from {}".format(args.name))

    # ------------------------DATASET CREATION--------------------
    dictionary = Dictionary.load_from_file(config.dict_path)
    if args.test_only:
        if dataset != "vqace":
            eval_dset = VQAFeatureDataset('test', dictionary, dataset)
        else:
            eval_dset = VQAFeatureDataset('cou', dictionary, dataset)
    else:
        train_dset = VQAFeatureDataset('train', dictionary, dataset)
        if dataset != "vqace":
            eval_dset = VQAFeatureDataset('test', dictionary, dataset)
        else:
            eval_dset = VQAFeatureDataset('all', dictionary, dataset)
            easy_dset = VQAFeatureDataset('easy', dictionary, dataset)
            hard_dset = VQAFeatureDataset('hard', dictionary, dataset)
            cou_dset = VQAFeatureDataset('cou', dictionary, dataset)
    # if config.train_set == 'train+val' and not args.test_only:
    #     train_dset = train_dset + eval_dset
    #     eval_dset = VQAFeatureDataset('test', dictionary)
    # if args.eval_only:
    #     eval_dset = VQAFeatureDataset('val', dictionary)

    tb_count = 0
    writer = SummaryWriter() # for visualization

    if not config.train_set == 'train+val' and 'LM' in args.loss_fn:
        utils.append_bias(train_dset, eval_dset, len(eval_dset.label2ans))

    # ------------------------MODEL CREATION------------------------
    constructor = 'build_{}'.format(args.model)
    model, metric_fc = getattr(base_model, constructor)(eval_dset, args.num_hid)
    model = model.cuda()
    metric_fc = metric_fc.cuda()
    model.w_emb.init_embedding(config.glove_embed_path)

    # model = nn.DataParallel(model).cuda()
    # 获取问题模态 (q_emb 和 q_net) 的参数
    question_params = list(model.q_emb.parameters()) + list(model.q_net.parameters())

    # 获取图像模态 (v_att 和 v_net) 的参数
    visual_params = list(model.v_att.parameters()) + list(model.v_net.parameters())

    # 获取融合模态 (fusion 和 weight) 的参数
    fusion_params = list(model.weight.parameters())

    # 获取 w_emb 参数
    word_embedding_params = list(model.w_emb.parameters())

    # 定义优化器，为每个部分设置不同的学习率
    optim = torch.optim.Adam([
        {'params': question_params, 'lr': args.lrq},        # 问题模态的学习率
        {'params': visual_params, 'lr': args.lrv},         # 图像模态的学习率
        {'params': fusion_params, 'lr': args.lrf},          # 融合模态的学习率
        {'params': word_embedding_params, 'lr': args.lr},  # w_emb 的学习率
        {'params': metric_fc.parameters(), 'lr': args.lr}  # w_emb 的学习率
    ])

    if args.GMS:
        gms = GMS(optim, reduction='mean')
        optim  = gms

    if args.loss_fn == 'Plain':
        loss_fn = Plain()
    else:
        raise RuntimeError('not implement for {}'.format(args.loss_fn))

    # ------------------------STATE CREATION------------------------
    eval_score, best_val_score, start_epoch, best_epoch = 0.0, 0.0, 0, 0
    tracker = utils.Tracker()
    if args.resume:
        model.load_state_dict(logs['model_state'])
        metric_fc.load_state_dict(logs['margin_model_state'])
        optim.load_state_dict(logs['optim_state'])
        if 'loss_state' in logs:
            loss_fn.load_state_dict(logs['loss_state'])
        start_epoch = logs['epoch']
        best_epoch = logs['epoch']
        best_val_score = logs['best_val_score']
        if args.fine_tune:
            print('best accuracy is {:.2f} in baseline'.format(100 * best_val_score))
            args.epochs = start_epoch + 10 # 10 more epochs
            for params in optim.param_groups:
                params['lr'] = config.ft_lr

            # if you want save your model with a new name
            if args.name_new:
                if 'log' not in args.name_new:
                    args.name = 'logs/' + args.name_new
                else:
                    args.name = args.name_new

    eval_loader = DataLoader(eval_dset,
                    args.batch_size, shuffle=False, num_workers=4)
    if dataset == "vqace":
        if args.test_only:
            eval_loader = DataLoader(eval_dset,
                    args.batch_size, shuffle=False, num_workers=4)
        else:
            easy_loader = DataLoader(easy_dset,
                        args.batch_size, shuffle=False, num_workers=4)
            hard_loader = DataLoader(hard_dset,
                        args.batch_size, shuffle=False, num_workers=4)
            cou_loader = DataLoader(cou_dset,
                        args.batch_size, shuffle=False, num_workers=4)
    if args.test_only or args.eval_only:
        model.eval()
        metric_fc.eval()
        evaluate(model, metric_fc, eval_loader, eval_dset, args, write=False)
    else:
        train_loader = DataLoader(
            train_dset, args.batch_size, shuffle=True, num_workers=4)
        for epoch in range(start_epoch, args.epochs):
            print("training epoch {:03d}".format(epoch))
            tb_count = train(model, metric_fc, optim, train_loader, loss_fn, tracker, writer, tb_count, epoch, args)

            if not (config.train_set == 'train+val' and epoch in range(args.epochs - 3)):
                # save for the last three epochs
                write = True if config.train_set == 'train+val' else False
                print("validating after epoch {:03d}".format(epoch))
                model.train(False)
                metric_fc.train(False)
                eval_score = evaluate(model, metric_fc, eval_loader, eval_dset, args, epoch, write=write)
                if dataset == "vqace":
                    easy_score = evaluate(model, metric_fc, easy_loader, easy_dset, args, epoch, write=False)
                    hard_score = evaluate(model, metric_fc, hard_loader, hard_dset, args, epoch, write=False)
                    cou_score = evaluate(model, metric_fc, cou_loader, cou_dset, args, epoch, write=False)
                model.train(True)
                metric_fc.train(True)
                print("eval score: {:.2f} \n".format(100 * eval_score))
                if dataset == "vqace":
                    print("easy score: {:.2f} \n".format(100 * easy_score))
                    print("hard score: {:.2f} \n".format(100 * hard_score))
                    print("cou score: {:.2f} \n".format(100 * cou_score))

            if eval_score > best_val_score:
                best_val_score = eval_score
                best_epoch = epoch
            results = {
                'epoch': epoch + 1,
                'best_val_score': best_val_score,
                'model_state': model.state_dict(),
                'optim_state': optim.state_dict(),
                'loss_state': loss_fn.state_dict(),
                'margin_model_state': metric_fc.state_dict()
            }
            if not args.not_save:
                torch.save(results, args.name)
        print("best accuracy {:.2f} on epoch {:03d}".format(100 * best_val_score, best_epoch))
