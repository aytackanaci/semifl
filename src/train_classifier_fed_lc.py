import argparse
import datetime
import models
import os
import shutil
import time
import torch
import torch.backends.cudnn as cudnn
import numpy as np
from config import cfg, process_args
from data import fetch_dataset, split_dataset, make_data_loader, separate_dataset, separate_dataset_su, \
    make_batchnorm_dataset_su, make_batchnorm_stats
from metrics import Metric
from modules import Server, ClientLabeled
from utils import save, to_device, process_control, process_dataset, make_optimizer, make_scheduler, resume, collate
from logger import make_logger

os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'
cudnn.benchmark = True
parser = argparse.ArgumentParser(description='cfg')
for k in cfg:
    exec('parser.add_argument(\'--{0}\', default=cfg[\'{0}\'], type=type(cfg[\'{0}\']))'.format(k))
parser.add_argument('--control_name', default=None, type=str)
args = vars(parser.parse_args())
process_args(args)


def main():
    process_control()
    seeds = list(range(cfg['init_seed'], cfg['init_seed'] + cfg['num_experiments']))
    for i in range(cfg['num_experiments']):
        model_tag_list = [str(seeds[i]), cfg['data_name'], cfg['model_name'], cfg['control_name']]
        cfg['model_tag'] = '_'.join([x for x in model_tag_list if x])
        print('Experiment: {}'.format(cfg['model_tag']))
        runExperiment()
    return


def runExperiment():
    cfg['seed'] = int(cfg['model_tag'].split('_')[0])
    torch.manual_seed(cfg['seed'])
    torch.cuda.manual_seed(cfg['seed'])
    labeled_dataset = fetch_dataset(cfg['data_name'])
    unlabeled_dataset = fetch_dataset(cfg['data_name'])
    process_dataset(labeled_dataset)
    labeled_dataset['train'], unlabeled_dataset['train'], supervised_idx = separate_dataset_su(labeled_dataset['train'],
                                                                                               unlabeled_dataset[
                                                                                                   'train'])
    data_loader = make_data_loader(labeled_dataset, 'global')
    model = eval('models.{}().to(cfg["device"])'.format(cfg['model_name']))
    optimizer = make_optimizer(model, 'local')
    scheduler = make_scheduler(optimizer, 'global')
    batchnorm_dataset = make_batchnorm_dataset_su(labeled_dataset['train'], unlabeled_dataset['train'])
    data_split_labeled = split_dataset(labeled_dataset, cfg['num_clients'], 'iid')
    data_split_unlabeled = split_dataset(unlabeled_dataset, cfg['num_clients'], cfg['data_split_mode'])
    metric = Metric({'train': ['Loss', 'Accuracy'], 'test': ['Loss', 'Accuracy']})
    if cfg['resume_mode'] == 1:
        result = resume(cfg['model_tag'])
        last_epoch = result['epoch']
        if last_epoch > 1:
            data_split_labeled = result['data_split_labeled']
            data_split_unlabeled = result['data_split_unlabeled']
            supervised_idx = result['supervised_idx']
            server = result['server']
            client = result['client']
            optimizer.load_state_dict(result['optimizer_state_dict'])
            scheduler.load_state_dict(result['scheduler_state_dict'])
            logger = result['logger']
        else:
            server = make_server(model)
            client = make_client(model, data_split_labeled, data_split_unlabeled)
            logger = make_logger('output/runs/train_{}'.format(cfg['model_tag']))
    else:
        last_epoch = 1
        server = make_server(model)
        client = make_client(model, data_split_labeled, data_split_unlabeled)
        logger = make_logger('output/runs/train_{}'.format(cfg['model_tag']))
    for epoch in range(last_epoch, cfg['global']['num_epochs'] + 1):
        train_client(batchnorm_dataset, labeled_dataset['train'], unlabeled_dataset['train'], server, client, optimizer,
                     metric, logger, epoch)
        logger.reset()
        server.update(client)
        scheduler.step()
        model.load_state_dict(server.model_state_dict)
        test_model = make_batchnorm_stats(batchnorm_dataset, model, 'global')
        test(data_loader['test'], test_model, metric, logger, epoch)
        result = {'cfg': cfg, 'epoch': epoch + 1, 'server': server, 'client': client,
                  'optimizer_state_dict': optimizer.state_dict(),
                  'scheduler_state_dict': scheduler.state_dict(),
                  'supervised_idx': supervised_idx, 'data_split_labeled': data_split_labeled,
                  'data_split_unlabeled': data_split_unlabeled, 'logger': logger}
        save(result, './output/model/{}_checkpoint.pt'.format(cfg['model_tag']))
        if metric.compare(logger.mean['test/{}'.format(metric.pivot_name)]):
            metric.update(logger.mean['test/{}'.format(metric.pivot_name)])
            shutil.copy('./output/model/{}_checkpoint.pt'.format(cfg['model_tag']),
                        './output/model/{}_best.pt'.format(cfg['model_tag']))
        logger.reset()
    return


def make_server(model):
    server = Server(model)
    return server


def make_client(model, data_split_labeled, data_split_unlabeled):
    client_id = torch.arange(cfg['num_clients'])
    client = [None for _ in range(cfg['num_clients'])]
    for m in range(len(client)):
        client[m] = ClientLabeled(client_id[m], model,
                                  {'train': data_split_labeled['train'][m], 'test': data_split_labeled['test'][m]},
                                  {'train': data_split_unlabeled['train'][m], 'test': data_split_unlabeled['test'][m]})
    return client


def train_client(batchnorm_dataset, labeled_dataset, unlabeled_dataset, server, client, optimizer, metric, logger,
                 epoch):
    logger.safe(True)
    num_active_clients = int(np.ceil(cfg['active_rate'] * cfg['num_clients']))
    client_id = torch.arange(cfg['num_clients'])[torch.randperm(cfg['num_clients'])[:num_active_clients]].tolist()
    for i in range(num_active_clients):
        client[client_id[i]].active = True
    server.distribute(batchnorm_dataset, client)
    num_active_clients = len(client_id)
    start_time = time.time()
    lr = optimizer.param_groups[0]['lr']
    for i in range(num_active_clients):
        m = client_id[i]
        labeled_dataset_m = separate_dataset(labeled_dataset, client[m].data_split_labeled['train'])
        unlabeled_dataset_m = separate_dataset(unlabeled_dataset, client[m].data_split_unlabeled['train'])
        dataset_m = client[m].make_dataset(labeled_dataset_m, unlabeled_dataset_m)
        if dataset_m is not None:
            client[m].active = True
            client[m].train(dataset_m, lr, metric, logger)
        else:
            client[m].active = False
        if i % int((num_active_clients * cfg['log_interval']) + 1) == 0:
            _time = (time.time() - start_time) / (i + 1)
            epoch_finished_time = datetime.timedelta(seconds=_time * (num_active_clients - i - 1))
            exp_finished_time = epoch_finished_time + datetime.timedelta(
                seconds=round((cfg['global']['num_epochs'] - epoch) * _time * num_active_clients))
            exp_progress = 100. * i / num_active_clients
            info = {'info': ['Model: {}'.format(cfg['model_tag']),
                             'Train Epoch (C): {}({:.0f}%)'.format(epoch, exp_progress),
                             'Learning rate: {:.6f}'.format(lr),
                             'ID: {}({}/{})'.format(client_id[i], i + 1, num_active_clients),
                             'Epoch Finished Time: {}'.format(epoch_finished_time),
                             'Experiment Finished Time: {}'.format(exp_finished_time)]}
            logger.append(info, 'train', mean=False)
            print(logger.write('train', metric.metric_name['train']))
    logger.safe(False)
    return


def test(data_loader, model, metric, logger, epoch):
    logger.safe(True)
    with torch.no_grad():
        model.train(False)
        for i, input in enumerate(data_loader):
            input = collate(input)
            input_size = input['data'].size(0)
            input = to_device(input, cfg['device'])
            output = model(input)
            output['loss'] = output['loss'].mean() if cfg['world_size'] > 1 else output['loss']
            evaluation = metric.evaluate(metric.metric_name['test'], input, output)
            logger.append(evaluation, 'test', input_size)
        info = {'info': ['Model: {}'.format(cfg['model_tag']), 'Test Epoch: {}({:.0f}%)'.format(epoch, 100.)]}
        logger.append(info, 'test', mean=False)
        print(logger.write('test', metric.metric_name['test']))
    logger.safe(False)
    return


if __name__ == "__main__":
    main()